"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import itertools
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
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast
from uuid import UUID

import capture_usage_claude as usage_claude
import capture_usage_cli as usage_cli
import capture_usage_codex as usage_codex
import capture_usage_models as usage_models
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
    BOUND_ROOT_POLICIES,
    EVIDENCE_BUNDLE_FILES,
    EVIDENCE_MANIFEST_NAME,
    EVIDENCE_MAX_FILE_BYTES,
    EVIDENCE_MAX_TOTAL_BYTES,
    EVIDENCE_PAYLOAD_FILES,
    EVIDENCE_ROOT_ENVIRONMENT_KEY,
    EVIDENCE_SCHEMA_VERSION,
    MAX_EVENT_BYTES,
    MAX_EVENT_COUNT,
    MAX_STREAM_BYTES,
    NATIVE_ROLE_EXCLUSIONS,
    PLAN_ROOT_ENVIRONMENT_KEY,
    VALIDATION_ENVIRONMENT_ROLES,
    VALIDATION_ROLE_COMMANDS,
    BoundedStreamError,
    BoundRoot,
    BoundRootKind,
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
    ValidationCommandKind,
    bounded_jsonl_lines,
    render_allowed_tools,
    render_validation_commands,
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
        tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high", expected_permission_mode="auto"
    )

    assert usage.usage_message_count == 1
    assert usage.input_tokens == 2
    assert usage.cached_input_tokens == 0
    assert usage.cache_creation_input_tokens == 36_369
    assert usage.output_tokens == 9
    assert transcript.read_bytes() == duplicated


def test_owned_transcript_tolerates_non_terminal_tool_activity_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13: a tool_use/tool_result pair around a non-terminal row parses cleanly.

    A byte-mutated committed fixture plants ``SENTINEL_TOOL_ACTIVITY`` in a
    non-terminal assistant row's ``tool_use`` block and in an inserted
    tool-result ``user`` row (an already-admitted row type). Parsing must
    succeed, produce the expected usage totals, and the handback must be
    drawn only from the terminal (file-order-last) assistant row's ``text``
    blocks — the sentinel must appear nowhere in the returned handback text.
    """
    session_id = "11111111-1111-4111-8111-111111111233"
    rows = _story_planner_rows(session_id, fixture="qa.jsonl")
    assistant_indices = [index for index, row in enumerate(rows) if row["type"] == "assistant"]
    assert len(assistant_indices) == 2
    non_terminal_index, terminal_index = assistant_indices
    assert non_terminal_index < terminal_index

    non_terminal_row = rows[non_terminal_index]
    non_terminal_row["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "toolu_sentinel",
            "name": "Bash",
            "input": {"command": "SENTINEL_TOOL_ACTIVITY"},
        }
    ]
    tool_result_row = {
        "parentUuid": non_terminal_row["uuid"],
        "isSidechain": False,
        "userType": "external",
        "cwd": "",
        "sessionId": session_id,
        "version": "2.1.233",
        "gitBranch": "",
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_sentinel",
                    "content": "SENTINEL_TOOL_ACTIVITY",
                }
            ],
        },
        "uuid": "11111111-1111-4111-8111-111111199999",
        "timestamp": "2026-08-18T00:00:01.500Z",
    }
    rows.insert(non_terminal_index + 1, tool_result_row)

    transcript, _ = _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)

    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        session_id,
        NativeClaudeRole.QA,
        "high",
        expected_permission_mode="dontAsk",
        require_handback=True,
    )
    assert usage.usage_message_count == 2
    assert usage.handback_text == "SYNTHETIC_QA_HANDOFF"
    assert usage.handback_text is not None and "SENTINEL_TOOL_ACTIVITY" not in usage.handback_text
    assert transcript.read_bytes() == _dump_rows(rows)


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
        usage_transcript.parse_owned_transcript(
            tmp_path, installed_session, role, "high", expected_permission_mode="auto"
        )
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
        tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high", expected_permission_mode="auto"
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
        )
    after = transcript.stat()
    assert (after.st_ino, after.st_mtime_ns, transcript.read_bytes()) == (
        before.st_ino,
        before.st_mtime_ns,
        mutated,
    )


@pytest.mark.parametrize(
    ("fixture", "role", "effort", "model", "totals", "permission_mode", "message_count"),
    [
        (
            "parent.jsonl",
            NativeClaudeRole.ENGINEER_BE,
            "high",
            "claude-sonnet-5",
            (2, 0, 36_369, 9),
            "auto",
            1,
        ),
        (
            "engineer-fe.jsonl",
            NativeClaudeRole.ENGINEER_FE,
            "high",
            "claude-sonnet-5",
            (2, 6_289, 29_307, 9),
            "auto",
            1,
        ),
        (
            "story-planner.jsonl",
            NativeClaudeRole.STORY_PLANNER,
            "high",
            "claude-opus-5",
            (2, 6, 4, 8),
            "dontAsk",
            2,
        ),
        (
            "safety-reviewer.jsonl",
            NativeClaudeRole.SAFETY_REVIEWER,
            "xhigh",
            "claude-opus-5",
            (2, 6, 4, 8),
            "dontAsk",
            2,
        ),
        (
            "security-reviewer.jsonl",
            NativeClaudeRole.SECURITY_REVIEWER,
            "high",
            "claude-sonnet-5",
            (2, 6, 4, 8),
            "dontAsk",
            2,
        ),
        (
            "qa.jsonl",
            NativeClaudeRole.QA,
            "high",
            "claude-sonnet-5",
            (2, 6, 4, 8),
            "dontAsk",
            2,
        ),
    ],
)
def test_owned_transcript_parses_every_committed_2_1_233_role_fixture(
    fixture: str,
    role: NativeClaudeRole,
    effort: str,
    model: str,
    totals: tuple[int, int, int, int],
    permission_mode: str,
    message_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every committed 2.1.233 transcript fixture parses to its exact role and totals."""
    content = (FIXTURES / "claude-2.1.233-transcript" / fixture).read_bytes()
    session_id = json.loads(content.splitlines()[0])["sessionId"]
    _, installed_session_id = _install_owned_transcript(tmp_path, monkeypatch, content, session_id)
    usage = usage_transcript.parse_owned_transcript(
        tmp_path, installed_session_id, role, effort, expected_permission_mode=permission_mode
    )
    assert usage.model == model
    assert usage.usage_message_count == message_count
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
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
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
            tmp_path,
            session_id,
            NativeClaudeRole.SECURITY_REVIEWER,
            "high",
            expected_permission_mode="dontAsk",
        )


def test_native_transcript_binds_model_and_effort_to_the_pinned_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript naming a wrong model family or a wrong effort fails closed."""
    planner_content = (FIXTURES / "claude-2.1.233-transcript" / "story-planner.jsonl").read_bytes()
    planner_session_id = json.loads(planner_content.splitlines()[0])["sessionId"]
    planner_pin = usage_cli._native_role_pin(NativeClaudeRole.STORY_PLANNER)  # pyright: ignore[reportPrivateUsage]
    assert planner_pin.model == "claude-opus-5"

    fable_content = planner_content.replace(b'"model":"claude-opus-5"', b'"model":"claude-fable-5"')
    case = tmp_path / "planner"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, fable_content, planner_session_id)
    usage = usage_transcript.parse_owned_transcript(
        case,
        session_id,
        NativeClaudeRole.STORY_PLANNER,
        planner_pin.effort,
        expected_permission_mode="dontAsk",
    )
    assert usage.model == "claude-fable-5" != planner_pin.model

    safety_content = (FIXTURES / "claude-2.1.233-transcript" / "safety-reviewer.jsonl").read_bytes()
    safety_session_id = json.loads(safety_content.splitlines()[0])["sessionId"]
    safety_pin = usage_cli._native_role_pin(NativeClaudeRole.SAFETY_REVIEWER)  # pyright: ignore[reportPrivateUsage]
    assert safety_pin.effort == "xhigh"

    downgraded = safety_content.replace(b'"effort":"xhigh"', b'"effort":"high"')
    case = tmp_path / "safety"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, downgraded, safety_session_id)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            case,
            session_id,
            NativeClaudeRole.SAFETY_REVIEWER,
            safety_pin.effort,
            expected_permission_mode="dontAsk",
        )

    case = tmp_path / "safety-matching"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, safety_content, safety_session_id)
    usage = usage_transcript.parse_owned_transcript(
        case,
        session_id,
        NativeClaudeRole.SAFETY_REVIEWER,
        safety_pin.effort,
        expected_permission_mode="dontAsk",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
            expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
                case,
                session_id,
                NativeClaudeRole.ENGINEER_BE,
                "high",
                expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
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
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
        )
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROWS", 500_000)
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROW_BYTES", 1)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
        )
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROW_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_BYTES", 1)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            expected_permission_mode="auto",
        )
    assert transcript.read_bytes() == content


@pytest.mark.parametrize("role", list(NativeClaudeRole))
def test_native_argv_is_exact_and_generic_measurement_stays_ephemeral(
    role: NativeClaudeRole,
) -> None:
    """D161 session persistence is native-only and no native stdout grammar survives.

    D168: also asserts the exact ``--allowedTools`` rule tuple, and that it is
    the final argv element, for every validation role (T1, T2, T3, T6).
    """
    session_id = "11111111-1111-4111-8111-111111111233"
    pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
    fake_root_by_kind = {
        BoundRootKind.VALIDATION: "/validated/root",
        BoundRootKind.PLAN: "/plan/root",
        BoundRootKind.EVIDENCE: "/evidence/root",
    }
    required_kind = next(
        (kind for kind, policy in BOUND_ROOT_POLICIES.items() if role in policy.required_roles),
        None,
    )
    bound_root = (
        BoundRoot(required_kind, fake_root_by_kind[required_kind])
        if required_kind is not None
        else None
    )
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude", role, pin.capability, pin.effort, session_id, bound_root
    )
    expected_middle = ["--add-dir", bound_root.path] if bound_root is not None else []
    expected_rules = (
        list(render_allowed_tools(role, bound_root.path))
        if bound_root is not None and bound_root.kind is BoundRootKind.VALIDATION
        else []
    )
    expected_tail = ["--allowedTools", *expected_rules] if expected_rules else []
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
        *expected_middle,
        "--permission-mode",
        "auto" if pin.capability is RoleCapability.WRITE else "dontAsk",
        "--effort",
        pin.effort,
        *expected_tail,
    ]
    if bound_root is not None and bound_root.kind is BoundRootKind.VALIDATION:
        assert argv[argv.index("--add-dir") + 2] == "--permission-mode"
        assert "--allowedTools" in argv
        assert argv[-1] == expected_rules[-1]
        assert argv.index("--allowedTools") + 1 + len(expected_rules) == len(argv)
    else:
        assert "--allowedTools" not in argv
        if bound_root is not None:
            assert argv[argv.index("--add-dir") + 2] == "--permission-mode"
    assert "--no-session-persistence" not in argv and "--output-format" not in argv
    generic = usage_cli._launch_argv(HarnessFamily.CLAUDE, "claude", "model", "high")  # pyright: ignore[reportPrivateUsage]
    assert "--no-session-persistence" in generic
    assert "--add-dir" not in generic
    assert "--allowedTools" not in generic
    generic_without_effort = usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CLAUDE, "claude", "model", None
    )
    assert "--effort" not in generic_without_effort


def test_native_argv_rejects_root_role_mismatch() -> None:
    """D167's argv builder rejects a validated root for a non-validation role and vice versa."""
    session_id = "11111111-1111-4111-8111-111111111233"
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
            "claude",
            NativeClaudeRole.ENGINEER_BE,
            RoleCapability.WRITE,
            "high",
            session_id,
            BoundRoot(BoundRootKind.VALIDATION, "/validated/root"),
        )
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
            "claude",
            NativeClaudeRole.QA,
            RoleCapability.READ_ONLY,
            "high",
            session_id,
            None,
        )
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
            "claude",
            NativeClaudeRole.QA,
            RoleCapability.READ_ONLY,
            "high",
            session_id,
            BoundRoot(BoundRootKind.PLAN, "/plan/root"),
        )


def test_render_allowed_tools_is_exact_literal_per_role() -> None:
    """T1: each validation role's rendered rule tuple equals the exact §2.2 literals."""
    root = "/validated/root"
    python = f"{root}/venv/bin/python"
    tmp = f"{root}/tmp"
    assert render_allowed_tools(NativeClaudeRole.QA, root) == (
        f"Bash({python} -m pytest:*)",
        f"Bash({python} -m pyright --pythonpath {python})",
        f"Bash({python} -m ruff check .)",
        f"Bash({python} -m ruff format --check .)",
    )
    assert render_allowed_tools(NativeClaudeRole.MCP_CONTRACT_CHECKER, root) == (
        f"Bash({python} -m pip show coffee-roaster-mcp)",
        f"Bash({python} -m pytest tests/test_mcp_client.py -q --basetemp {tmp}/pytest)",
    )
    assert render_allowed_tools(NativeClaudeRole.SIM_ROAST_RUNNER, root) == (
        f"Bash({python} -m pytest tests/test_milestone1.py "
        f"tests/test_milestone1_real_mcp.py -q --basetemp {tmp}/pytest)",
    )
    assert render_validation_commands(NativeClaudeRole.QA, root) == (
        f"{python} -m pytest",
        f"{python} -m pyright --pythonpath {python}",
        f"{python} -m ruff check .",
        f"{python} -m ruff format --check .",
    )


_NON_VALIDATION_ROLES = [
    role for role in NativeClaudeRole if role not in usage_cli.VALIDATION_ENVIRONMENT_ROLES
]


@pytest.mark.parametrize("role", _NON_VALIDATION_ROLES)
def test_render_allowed_tools_empty_for_non_validation_roles(role: NativeClaudeRole) -> None:
    """T2: every non-validation role renders no commands and no rules."""
    assert render_validation_commands(role, "/validated/root") == ()
    assert render_allowed_tools(role, "/validated/root") == ()


def test_render_allowed_tools_uses_the_validated_resolved_root_not_the_raw_argument(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T5: argv rules are rendered from the validated resolved root, never the raw one."""
    base = tmp_path_factory.mktemp("render-symlink")
    actual = base / "actual"
    actual.mkdir()
    root = _build_validation_root(actual / "root")
    link = base / "link"
    link.symlink_to(actual)
    raw = str(link / "root")
    resolved = usage_cli._validate_validation_root(raw)  # pyright: ignore[reportPrivateUsage]
    assert resolved == os.path.realpath(root)
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.QA,
        RoleCapability.READ_ONLY,
        "high",
        "11111111-1111-4111-8111-111111111233",
        BoundRoot(BoundRootKind.VALIDATION, resolved),
    )
    rules = argv[argv.index("--allowedTools") + 1 :]
    assert all(str(link) not in rule for rule in rules)
    assert any(resolved in rule for rule in rules)


def test_validation_role_commands_table_is_a_closed_positive_grammar() -> None:
    """T7: every template is well-formed and uses only the closed module set."""
    allowed_modules = {"pytest", "pyright", "ruff", "pip"}
    forbidden_fragments = ("(", ")", ";", "&", "|", "$", "`")
    prefix_entries: list[tuple[NativeClaudeRole, usage_models.ValidationCommand]] = []
    for role, commands in VALIDATION_ROLE_COMMANDS.items():
        for command in commands:
            assert command.template.startswith("{python} -m ")
            module = command.template.removeprefix("{python} -m ").split(" ", 1)[0]
            assert module in allowed_modules
            if module == "pip":
                assert command.template == "{python} -m pip show coffee-roaster-mcp"
            if command.kind is ValidationCommandKind.PREFIX:
                prefix_entries.append((role, command))
            rendered = command.template.format(python="/r/venv/bin/python", tmp="/r/tmp")
            assert rendered.count("*") == 0
            for fragment in forbidden_fragments:
                assert fragment not in rendered
    expected_prefix_entry = (NativeClaudeRole.QA, VALIDATION_ROLE_COMMANDS[NativeClaudeRole.QA][0])
    assert prefix_entries == [expected_prefix_entry]
    for role, commands in VALIDATION_ROLE_COMMANDS.items():
        rules = render_allowed_tools(role, "/r")
        # Exactly one rendered rule per table entry — no bare or extra rule.
        assert len(rules) == len(commands)
        for command, rule in zip(commands, rules, strict=True):
            assert rule.startswith("Bash(") and rule.endswith(")")
            inner = rule.removeprefix("Bash(").removesuffix(")")
            assert inner != "*"
            assert inner.startswith("/r/venv/bin/python -m ")
            assert inner.count("*") <= 1
            if command.kind is ValidationCommandKind.PREFIX:
                assert inner.endswith(":*")
                assert inner.count(":*") == 1
            else:
                assert not inner.endswith(":*")
                assert "*" not in inner


def test_validation_role_commands_table_keys_equal_validation_environment_roles() -> None:
    """T8: the table's keys are exactly the three validation roles, no more, no fewer."""
    assert set(VALIDATION_ROLE_COMMANDS) == usage_cli.VALIDATION_ENVIRONMENT_ROLES
    for role in VALIDATION_ROLE_COMMANDS:
        pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
        assert pin.capability is RoleCapability.READ_ONLY
        path = Path(".claude") / "agents" / f"{role.value}.md"
        tools_line = next(
            line for line in path.read_text().splitlines() if line.startswith("tools: ")
        )
        tools = {token.strip() for token in tools_line.removeprefix("tools: ").split(",")}
        assert "Bash" in tools
        assert not ({"Edit", "Write"} & tools)


def test_native_claude_argv_never_revalidates_the_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """T9: the argv builder performs no second root validation — it trusts its input."""

    def fail(_raw: str) -> str:
        raise AssertionError("argv builder must not re-validate the root")

    monkeypatch.setattr(usage_cli, "_validate_validation_root", fail)
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.QA,
        RoleCapability.READ_ONLY,
        "high",
        "11111111-1111-4111-8111-111111111233",
        BoundRoot(BoundRootKind.VALIDATION, "/validated/root"),
    )
    assert "--allowedTools" in argv


@pytest.mark.parametrize(
    "flag", ["--allowed-tools", "--allowedTools", "--tools", "--permission-mode", "--add-dir"]
)
def test_no_caller_permission_surface_options_exist_on_native(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """T10: no permission-surface option is caller-facing on ``run-native-claude``."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args([*_native_cli_args(role="qa"), flag, "value"])
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag", ["--allowed-tools", "--allowedTools", "--tools", "--permission-mode", "--add-dir"]
)
def test_no_caller_permission_surface_options_exist_on_run(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """T10: no permission-surface option is caller-facing on the generic ``run`` command."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [
                "run",
                "--harness",
                "claude",
                "--prompt-file",
                "prompt",
                "--task-id",
                "816",
                "--slice-id",
                "s1",
                "--role",
                "measurement",
                "--model",
                "model",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/816",
                "--base-sha",
                "4c1ac63",
                "--head-sha",
                "7d60f41",
                flag,
                "value",
            ]
        )
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


def test_native_argv_rejects_empty_rendered_rule_tuple_before_provider_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T11: an empty rendered rule tuple fails closed and never touches ``shutil.which``."""

    def empty_render(_role: NativeClaudeRole, _root: str) -> tuple[str, ...]:
        return ()

    def no_which(_name: str) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(usage_cli, "render_allowed_tools", empty_render)
    monkeypatch.setattr(usage_cli.shutil, "which", no_which)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
            "claude",
            NativeClaudeRole.QA,
            RoleCapability.READ_ONLY,
            "high",
            "11111111-1111-4111-8111-111111111233",
            BoundRoot(BoundRootKind.VALIDATION, "/validated/root"),
        )


def test_no_caller_add_dir_option_exists(capsys: pytest.CaptureFixture[str]) -> None:
    """The closed CLI grammar exposes no ``--add-dir`` option on any subcommand.

    Every other required argument is supplied so the failure is specifically
    an unrecognized ``--add-dir``, not an unrelated missing-argument error
    that would pass even if a caller-facing ``--add-dir`` option existed.
    """
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [*_native_cli_args(role="qa"), "--add-dir", "/somewhere"]
        )
    assert "unrecognized arguments: --add-dir" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [
                "run",
                "--harness",
                "claude",
                "--prompt-file",
                "prompt",
                "--task-id",
                "816",
                "--slice-id",
                "s1",
                "--role",
                "measurement",
                "--model",
                "model",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/816",
                "--base-sha",
                "4c1ac63",
                "--head-sha",
                "7d60f41",
                "--add-dir",
                "/somewhere",
            ]
        )
    assert "unrecognized arguments: --add-dir" in capsys.readouterr().err


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


def _native_cli_args(
    role: str = "engineer-be",
    *,
    validation_root: str | None = None,
    plan_root: str | None = None,
    plan_sha: str | None = None,
    evidence_root: str | None = None,
    evidence_pr: int | None = None,
    base_sha: str = "4c1ac63",
) -> list[str]:
    """Return closed native command metadata for provider-free tests."""
    arguments = [
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
        base_sha,
    ]
    if validation_root is not None:
        arguments.extend(["--validation-root", validation_root])
    if plan_root is not None:
        arguments.extend(["--plan-root", plan_root])
    if plan_sha is not None:
        arguments.extend(["--plan-sha", plan_sha])
    if evidence_root is not None:
        arguments.extend(["--evidence-root", evidence_root])
    if evidence_pr is not None:
        arguments.extend(["--evidence-pr", str(evidence_pr)])
    return arguments


def _request(
    *,
    validation_root: str | None = None,
    plan_root: str | None = None,
    plan_sha: str | None = None,
    evidence_root: str | None = None,
    evidence_pr: int | None = None,
) -> usage_cli._BoundRootRequest:  # pyright: ignore[reportPrivateUsage]
    """Build a closed bound-root request for direct-call presence/resolution tests."""
    return usage_cli._BoundRootRequest(  # pyright: ignore[reportPrivateUsage]
        validation_root=validation_root,
        plan_root=plan_root,
        plan_sha=plan_sha,
        evidence_root=evidence_root,
        evidence_pr=evidence_pr,
    )


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
        None,
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


def test_read_only_nonzero_exit_records_failure_without_handback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed READ_ONLY child retains metadata but cannot emit a success handback."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            code=2,
            transcript=_read_only_transcript_bytes("story-planner"),
        ),
    )
    assert (
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
        == 0
    )
    assert capsys.readouterr().out == ""
    record = USAGE_RECORD_ADAPTER.validate_json(Path(".agent-usage/usage.jsonl").read_text())
    assert isinstance(record, NativeWorkerUsageRecord)
    assert not record.success and record.exit_code == 2


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


def test_held_plan_descriptor_closes_after_pre_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved PLAN descriptor closes even when config preflight rejects before launch."""
    _configure_read_only_native_launcher(tmp_path, monkeypatch)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    bound = BoundRoot(kind=BoundRootKind.PLAN, path=str(tmp_path), descriptor=descriptor)
    launched = False

    def resolved(*_args: object, **_kwargs: object) -> usage_cli._NativeLaunchEnvironment:  # pyright: ignore[reportPrivateUsage]
        return usage_cli._NativeLaunchEnvironment(environment={}, bound_root=bound)  # pyright: ignore[reportPrivateUsage]

    def no_provider(_family: HarnessFamily) -> str:
        nonlocal launched
        launched = True
        return "claude"

    monkeypatch.setattr(usage_cli, "_resolve_native_environment", resolved)
    monkeypatch.setattr(usage_cli, "_resolved_executable", no_provider)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "SENTINEL_CONFIG")
    try:
        with pytest.raises(SystemExit, match="config directory is not permitted"):
            main(
                _native_cli_args(
                    role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
                )
            )
        assert not launched
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def test_held_plan_descriptor_closes_after_successful_read_only_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held root survives reattestation but closes after a successful READ_ONLY record."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    expected_inode = os.fstat(descriptor).st_ino
    reattested = False

    def reattest() -> None:
        nonlocal reattested
        assert os.fstat(descriptor).st_ino == expected_inode
        reattested = True

    bound = BoundRoot(
        kind=BoundRootKind.PLAN, path=str(tmp_path), reattest=reattest, descriptor=descriptor
    )

    def resolved(*_args: object, **_kwargs: object) -> usage_cli._NativeLaunchEnvironment:  # pyright: ignore[reportPrivateUsage]
        return usage_cli._NativeLaunchEnvironment(environment={}, bound_root=bound)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(
        usage_cli,
        "_resolve_native_environment",
        resolved,
    )
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("story-planner"),
        ),
    )
    try:
        assert (
            main(
                _native_cli_args(
                    role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
                )
            )
            == 0
        )
        assert reattested
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)


@pytest.mark.parametrize("error_message", ["plan root is invalid", "evidence bundle is invalid"])
def test_open_bound_root_descriptor_maps_path_failures_without_leakage(
    monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    """PLAN/EVIDENCE path-open failures keep their fixed path-free error."""

    def failing_open(*_args: object, **_kwargs: object) -> int:
        raise OSError("SENTINEL_ROOT_OPEN_FAILURE")

    monkeypatch.setattr(usage_cli.os, "open", failing_open)
    with pytest.raises(CaptureUsageError, match=error_message) as error:
        usage_cli._open_bound_root_descriptor(  # pyright: ignore[reportPrivateUsage]
            "/SENTINEL_ROOT_PATH", error_message=error_message
        )
    assert "SENTINEL_ROOT" not in str(error.value)


@pytest.mark.parametrize("error_message", ["plan root is invalid", "evidence bundle is invalid"])
def test_reattest_bound_root_maps_held_descriptor_fstat_failures_without_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    """PLAN/EVIDENCE held-descriptor failures fail closed without leaking filesystem details."""
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    real_fstat = usage_cli.os.fstat

    def failing_held_fstat(candidate: int) -> os.stat_result:
        if candidate == descriptor:
            raise OSError("SENTINEL_HELD_FSTAT_FAILURE")
        return real_fstat(candidate)

    monkeypatch.setattr(usage_cli.os, "fstat", failing_held_fstat)
    try:
        with pytest.raises(CaptureUsageError, match=error_message) as error:
            usage_cli._reattest_bound_root_path(  # pyright: ignore[reportPrivateUsage]
                descriptor, str(tmp_path), error_message=error_message
            )
        assert "SENTINEL_HELD_FSTAT_FAILURE" not in str(error.value)
        assert str(tmp_path) not in str(error.value)
    finally:
        with suppress(OSError):
            os.close(descriptor)


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


# ---------------------------------------------------------------------------
# D166 round-7 repair: native permission-mode freeze, the bounded READ_ONLY
# handback channel, and the parent-provisioned external validation root.
# ---------------------------------------------------------------------------


def _story_planner_rows(
    session_id: str, *, fixture: str = "story-planner.jsonl"
) -> list[dict[str, Any]]:
    """Load one committed READ_ONLY ``dontAsk`` fixture's rows bound to one session."""
    content = (FIXTURES / "claude-2.1.233-transcript" / fixture).read_bytes()
    rows: list[dict[str, Any]] = [json.loads(line) for line in content.splitlines()]
    original = rows[0]["sessionId"]
    for row in rows:
        if row.get("sessionId") == original:
            row["sessionId"] = session_id
    return rows


def _dump_rows(rows: list[dict[str, Any]]) -> bytes:
    """Serialize rows back to one newline-terminated JSONL transcript."""
    return b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"


_HANDBACK_UNSET = object()


def _install_story_planner_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminal_content: object = _HANDBACK_UNSET,
    stop_reason: object = _HANDBACK_UNSET,
    session_id: str = "71111111-1111-4111-8111-111111111111",
) -> tuple[Path, str]:
    """Install a READ_ONLY ``dontAsk`` transcript with a controllable terminal turn."""
    rows = _story_planner_rows(session_id)
    terminal_message = rows[-1]["message"]
    if terminal_content is not _HANDBACK_UNSET:
        terminal_message["content"] = terminal_content
    if stop_reason is not _HANDBACK_UNSET:
        terminal_message["stop_reason"] = stop_reason
    _, installed_session_id = _install_owned_transcript(
        tmp_path, monkeypatch, _dump_rows(rows), session_id
    )
    return tmp_path, installed_session_id


def test_handback_extracts_ordered_text_blocks_and_skips_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handback is the ordered text-block concatenation; thinking never contributes."""
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=[
            {"type": "thinking", "thinking": "SENTINEL_THINK_IN_TERMINAL", "signature": "s"},
            {"type": "text", "text": "PART_ONE "},
            {"type": "text", "text": "PART_TWO"},
        ],
    )
    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        session_id,
        NativeClaudeRole.STORY_PLANNER,
        "high",
        expected_permission_mode="dontAsk",
        require_handback=True,
    )
    assert usage.handback_text == "PART_ONE PART_TWO"
    assert "SENTINEL_THINK_IN_TERMINAL" not in (usage.handback_text or "")


def test_handback_ignores_content_in_earlier_assistant_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the terminal assistant row's content ever reaches the handback."""
    session_id = "71111111-1111-4111-8111-111111111112"
    rows = _story_planner_rows(session_id)
    rows[-2]["message"]["content"] = [{"type": "text", "text": "SENTINEL_EARLIER_ROW_TEXT"}]
    _, installed = _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)
    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        installed,
        NativeClaudeRole.STORY_PLANNER,
        "high",
        expected_permission_mode="dontAsk",
        require_handback=True,
    )
    assert "SENTINEL_EARLIER_ROW_TEXT" not in (usage.handback_text or "")


@pytest.mark.parametrize("stop_reason", ["tool_use", "max_tokens", None, 5])
def test_handback_requires_end_turn_terminal_stop_reason(
    stop_reason: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any terminal stop reason other than the exact ``end_turn`` string fails closed."""
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        stop_reason=stop_reason,
        session_id="71111111-1111-4111-8111-111111111113",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_handback_rejects_tool_use_block_in_terminal_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal turn ending in a tool call produced no final answer and fails closed."""
    tool_use_content: list[dict[str, object]] = [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
    ]
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=tool_use_content,
        session_id="71111111-1111-4111-8111-111111111114",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_handback_block_allowlist_is_closed_to_text_and_thinking_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block outside the frozen ``{text, thinking}`` set never contributes, even if shaped
    exactly like a valid text block otherwise (isolates the block-type allowlist guard)."""
    disguised_content: list[dict[str, object]] = [
        {"type": "tool_use", "text": "SHOULD_NEVER_BE_TREATED_AS_TEXT"}
    ]
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=disguised_content,
        session_id="71111111-1111-4111-8111-111111111119",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


@pytest.mark.parametrize(
    "terminal_content",
    [
        [],
        [{"type": "text", "text": "   \t\n  "}],
        "not-a-list",
        ["not-a-dict-block"],
    ],
    ids=["empty-content", "whitespace-only-text", "non-list-content", "non-dict-block"],
)
def test_handback_rejects_empty_or_malformed_content(
    terminal_content: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty content, whitespace-only text, and non-list content all fail closed."""
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=terminal_content,
        session_id="71111111-1111-4111-8111-111111111115",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_handback_rejects_oversize_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Handback text whose UTF-8 encoding exceeds the bound fails closed, never truncated."""
    monkeypatch.setattr(usage_transcript, "MAX_HANDBACK_BYTES", 8)
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=[{"type": "text", "text": "0123456789"}],
        session_id="71111111-1111-4111-8111-111111111116",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_handback_rejects_text_block_with_unfrozen_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``text`` block carrying any key outside the frozen observed set fails closed."""
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content=[{"type": "text", "text": "hi", "extra": "SENTINEL"}],
        session_id="71111111-1111-4111-8111-111111111117",
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_handback_is_none_and_unread_when_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``require_handback=False`` never reads content, even malformed content (I3)."""
    tmp_path, session_id = _install_story_planner_transcript(
        tmp_path,
        monkeypatch,
        terminal_content="INVALID_NON_LIST_CONTENT_THAT_WOULD_FAIL_IF_READ",
        session_id="71111111-1111-4111-8111-111111111118",
    )
    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        session_id,
        NativeClaudeRole.STORY_PLANNER,
        "high",
        expected_permission_mode="dontAsk",
    )
    assert usage.handback_text is None


def _configure_read_only_native_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[tuple[list[str], dict[str, object]]]]:
    """Install a READ_ONLY-shaped native launcher whose post-exit head equals its base."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)

    def fixed_attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        del post_exit
        return "4c1ac63"

    monkeypatch.setattr(usage_cli, "_validate_native_worktree", fixed_attestation)
    return project, observed


_STUB_PLAN_ROOT = "/plan/root"
_STUB_PLAN_SHA = "1" * 40


def _stub_plan_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass real plan-root git validation for tests unrelated to D169 plan behavior."""

    def fixed_validate_plan_root(raw: str, sha: str | None) -> BoundRoot:
        assert raw == _STUB_PLAN_ROOT
        assert sha == _STUB_PLAN_SHA
        return BoundRoot(kind=BoundRootKind.PLAN, path=_STUB_PLAN_ROOT, reattest=lambda: None)

    monkeypatch.setattr(usage_cli, "_validate_plan_root", fixed_validate_plan_root)


_READ_ONLY_FIXTURE_BY_ROLE = {
    "story-planner": "story-planner.jsonl",
    "qa": "qa.jsonl",
    "safety-reviewer": "safety-reviewer.jsonl",
    "security-reviewer": "security-reviewer.jsonl",
}


def _read_only_transcript_bytes(
    role_value: str,
    *,
    session_id: str = _NATIVE_SESSION_ID,
    text: str = "SYNTHETIC_STORY_HANDOFF",
) -> bytes:
    """Return one committed READ_ONLY fixture rebound to one session with fixed text."""
    fixture = _READ_ONLY_FIXTURE_BY_ROLE[role_value]
    content = (FIXTURES / "claude-2.1.233-transcript" / fixture).read_bytes()
    rows: list[dict[str, Any]] = [json.loads(line) for line in content.splitlines()]
    original = rows[0]["sessionId"]
    for row in rows:
        if row.get("sessionId") == original:
            row["sessionId"] = session_id
    rows[-1]["message"]["content"] = [{"type": "text", "text": text}]
    return _dump_rows(rows)


def _read_only_transcript_bytes_rebranded(
    source_role: str,
    target_role: str,
    *,
    session_id: str = _NATIVE_SESSION_ID,
    text: str = "SYNTHETIC_STORY_HANDOFF",
) -> bytes:
    """Rebrand a committed same-model/effort fixture's ``agentSetting`` to another role.

    Used only for roles with no committed transcript fixture (e.g. ``pr-triage``,
    D169) that share a model/effort pin with an existing fixture's role — adds
    no new fixture file (§3.8), just a runtime field substitution identical in
    spirit to the existing session-id and terminal-text rebind above.
    """
    fixture = _READ_ONLY_FIXTURE_BY_ROLE[source_role]
    content = (FIXTURES / "claude-2.1.233-transcript" / fixture).read_bytes()
    rows: list[dict[str, Any]] = [json.loads(line) for line in content.splitlines()]
    original = rows[0]["sessionId"]
    for row in rows:
        if row.get("sessionId") == original:
            row["sessionId"] = session_id
        if row.get("type") == "agent-setting" and row.get("agentSetting") == source_role:
            row["agentSetting"] = target_role
    rows[-1]["message"]["content"] = [{"type": "text", "text": text}]
    return _dump_rows(rows)


def test_native_read_only_emits_bounded_handback_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful READ_ONLY run emits exactly one framed, integrity-checkable stdout line."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("story-planner", text="SYNTHETIC_STORY_HANDOFF"),
        ),
    )
    assert (
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
        == 0
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert list(payload) == [
        "handback_schema_version",
        "tool_version",
        "native_role",
        "role_capability",
        "session_id",
        "task_id",
        "slice_id",
        "byte_length",
        "sha256",
        "text",
    ]
    assert payload["handback_schema_version"] == 1
    assert payload["native_role"] == "story-planner"
    assert payload["role_capability"] == "READ_ONLY"
    assert payload["session_id"] == _NATIVE_SESSION_ID
    assert payload["task_id"] == "816"
    assert payload["slice_id"] == "native-1"
    assert payload["text"] == "SYNTHETIC_STORY_HANDOFF"
    encoded = payload["text"].encode("utf-8")
    assert payload["byte_length"] == len(encoded)
    assert payload["sha256"] == hashlib.sha256(encoded).hexdigest()


def test_native_write_role_emits_nothing_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful WRITE run appends its record and writes nothing to stdout."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_native_transcript_bytes()),
    )
    assert main(_native_cli_args()) == 0
    assert capsys.readouterr().out == ""


def test_native_read_only_no_emission_on_post_exit_attestation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forced post-exit attestation failure leaves stdout empty and appends no record."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)

    def failing_attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        if post_exit:
            raise CaptureUsageError("native worktree attestation failed")
        return "4c1ac63"

    monkeypatch.setattr(usage_cli, "_validate_native_worktree", failing_attestation)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project, observed, processes, transcript=_read_only_transcript_bytes("story-planner")
        ),
    )
    with pytest.raises(SystemExit, match="native worktree attestation failed"):
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
    assert capsys.readouterr().out == ""
    assert not Path(".agent-usage/usage.jsonl").exists()


def test_native_read_only_no_emission_on_sink_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forced sink-append failure leaves stdout empty even with a complete transcript."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project, observed, processes, transcript=_read_only_transcript_bytes("story-planner")
        ),
    )

    def failing_append(_path: Path, _record: object) -> None:
        raise CaptureUsageError("could not append usage record")

    monkeypatch.setattr(usage_cli, "append_record", failing_append)
    with pytest.raises(SystemExit, match="could not append usage record"):
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
    assert capsys.readouterr().out == ""


def test_native_read_only_handback_absent_from_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The handback sentinel appears nowhere durable: sink, worktree files, or stderr."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    sentinel = "SENTINEL_HANDBACK_TEXT_MUST_STAY_LOCAL"
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("story-planner", text=sentinel),
        ),
    )
    assert (
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
        == 0
    )
    raw = Path(".agent-usage/usage.jsonl").read_text()
    assert sentinel not in raw
    for path in tmp_path.rglob("*"):
        if path.is_file() and "home" not in path.relative_to(tmp_path).parts:
            assert sentinel not in path.read_text(errors="ignore")
    captured = capsys.readouterr()
    assert sentinel not in captured.err
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.task_id == "816" and record.slice_id == "native-1"


def test_native_read_only_handback_framing_survives_adversarial_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Newlines, braces, delimiter-like text, and non-ASCII stay one safe ASCII stdout line."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    adversarial = "line one\nline two}<<<END>>>é中"
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("story-planner", text=adversarial),
        ),
    )
    assert (
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
        == 0
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert all(ord(character) < 128 for character in captured.out)
    payload = json.loads(lines[0])
    assert payload["text"] == adversarial


def test_native_read_only_oversize_handback_yields_no_record_or_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An oversize terminal turn fails closed end-to-end: empty stdout, no sink record."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    monkeypatch.setattr(usage_transcript, "MAX_HANDBACK_BYTES", 4)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("story-planner", text="0123456789"),
        ),
    )
    with pytest.raises(SystemExit, match="native Claude transcript is invalid"):
        main(
            _native_cli_args(
                role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
            )
        )
    assert capsys.readouterr().out == ""
    assert not Path(".agent-usage/usage.jsonl").exists()


@pytest.mark.parametrize("mutated_value", ["plan", "auto", 5, None])
def test_transcript_permission_mode_attestation_rejects_mismatch(
    mutated_value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any row's ``permissionMode`` must equal the capability-derived expected value exactly."""
    session_id = "72222222-2222-4222-8222-222222222222"
    rows = _story_planner_rows(session_id)
    for row in rows:
        if row.get("type") == "user":
            row["permissionMode"] = mutated_value
    _, installed = _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            installed,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
        )


def test_transcript_permission_mode_attestation_rejects_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript with no observed capability-derived permission mode fails closed."""
    session_id = "72222222-2222-4222-8222-222222222223"
    rows = _story_planner_rows(session_id)
    for row in rows:
        row.pop("permissionMode", None)
    _, installed = _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            installed,
            NativeClaudeRole.STORY_PLANNER,
            "high",
            expected_permission_mode="dontAsk",
        )


def _frontmatter_with_permission_mode(
    role_file: str, value: str | None, *, duplicate: bool = False
) -> bytes:
    """Return one role file's bytes with its ``permissionMode`` line replaced or injected."""
    path = Path(".claude") / "agents" / role_file
    text = path.read_bytes().decode()
    lines = text.splitlines()
    end = lines.index("---", 1)
    frontmatter = [line for line in lines[1:end] if not line.startswith("permissionMode: ")]
    if value is not None:
        frontmatter.append(f"permissionMode: {value}")
        if duplicate:
            frontmatter.append(f"permissionMode: {value}")
    new_lines = [lines[0], *frontmatter, "---", *lines[end + 1 :]]
    return ("\n".join(new_lines) + "\n").encode()


@pytest.mark.parametrize(
    ("role_file", "role", "value", "duplicate"),
    [
        ("story-planner.md", NativeClaudeRole.STORY_PLANNER, "plan", False),
        ("story-planner.md", NativeClaudeRole.STORY_PLANNER, "auto", False),
        ("engineer-be.md", NativeClaudeRole.ENGINEER_BE, "dontAsk", False),
        ("story-planner.md", NativeClaudeRole.STORY_PLANNER, "dontAsk", True),
    ],
    ids=["read-only-plan", "read-only-auto", "write-dontask", "duplicate-line"],
)
def test_native_role_frontmatter_permission_mode_rule_rejects(
    role_file: str,
    role: NativeClaudeRole,
    value: str,
    duplicate: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frontmatter permissionMode mismatched to capability, or duplicated, fails closed."""
    mutated = _frontmatter_with_permission_mode(role_file, value, duplicate=duplicate)

    def mutated_input(_path: Path) -> bytes:
        return mutated

    monkeypatch.setattr(usage_cli, "_input_bytes", mutated_input)
    with pytest.raises(CaptureUsageError, match="frontmatter is invalid"):
        usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]


def test_permission_mode_frontmatter_key_is_exactly_the_two_planning_roles() -> None:
    """Only the two Opus/high planning roles pin an explicit frontmatter ``permissionMode``."""
    agents_dir = Path(__file__).resolve().parents[1] / ".claude" / "agents"
    with_key: dict[str, str] = {}
    for path in sorted(agents_dir.glob("*.md")):
        lines = [
            line for line in path.read_text().splitlines() if line.startswith("permissionMode: ")
        ]
        if lines:
            assert len(lines) == 1
            with_key[path.stem] = lines[0].removeprefix("permissionMode: ")
    assert with_key == {"story-planner": "dontAsk", "planning-architect": "dontAsk"}


def test_generic_run_argv_and_authority_still_pin_plan_mode() -> None:
    """The generic ``run`` path stays on the unrelated, untouched ``plan`` boundary."""
    generic = usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CLAUDE, "claude", "model", "high"
    )
    assert "--tools" in generic and generic[generic.index("--tools") + 1] == ""
    assert (
        "--permission-mode" in generic and generic[generic.index("--permission-mode") + 1] == "plan"
    )
    assert frozenset({"plan"}) == usage_claude.CLAUDE_PERMISSION_MODES


def _mkdir_exact(path: Path, mode: int) -> None:
    """Create a directory with an exact permission bit-set, independent of umask."""
    path.mkdir()
    os.chmod(path, mode)


def _build_validation_root(root: Path) -> Path:
    """Build one fully valid parent-provisioned validation root at ``root``."""
    _mkdir_exact(root, 0o700)
    _mkdir_exact(root / "cache", 0o700)
    _mkdir_exact(root / "tmp", 0o700)
    venv = root / "venv"
    venv.mkdir()
    os.chmod(venv, 0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    bin_dir = venv / "bin"
    bin_dir.mkdir()
    interpreter = bin_dir / "python"
    interpreter.write_bytes(b"#!/bin/sh\necho fake\n")
    interpreter.chmod(0o755)
    return root


@pytest.mark.parametrize("role", list(NativeClaudeRole))
def test_validation_root_presence_is_required_and_forbidden_correctly(
    role: NativeClaudeRole, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``--validation-root`` is required for exactly the three validation roles."""
    requires_root = role in usage_cli.VALIDATION_ENVIRONMENT_ROLES
    supplied = (
        None
        if requires_root
        else str(_build_validation_root(tmp_path_factory.mktemp("base") / "root"))
    )
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
            role, _request(validation_root=supplied), attested_head="0" * 40
        )


def test_validation_root_check_runs_before_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validation role missing ``--validation-root`` never reaches ``shutil.which``."""
    _configure_native_launcher(tmp_path, monkeypatch)

    def no_which(_name: str) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(usage_cli.shutil, "which", no_which)
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(_native_cli_args(role="qa"))


def test_validation_environment_binds_exact_map_and_strips_for_others(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed 11-key map binds exactly under the root; other roles see none of it."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    for key in usage_cli._VALIDATION_ENVIRONMENT_KEYS:  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv(key, "SENTINEL_PRESET")
    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.QA, _request(validation_root=str(root)), attested_head="0" * 40
    )
    environment = launch_environment.environment
    resolved_root = os.path.realpath(root)
    assert launch_environment.bound_root is not None
    assert launch_environment.bound_root.kind is BoundRootKind.VALIDATION
    assert launch_environment.bound_root.path == resolved_root
    expected = {
        "ROASTPILOT_VALIDATION_ROOT": resolved_root,
        "ROASTPILOT_VALIDATION_PYTHON": os.path.join(resolved_root, "venv", "bin", "python"),
        "ROASTPILOT_VALIDATION_TMP": os.path.join(resolved_root, "tmp"),
        "TMPDIR": os.path.join(resolved_root, "tmp"),
        "XDG_CACHE_HOME": os.path.join(resolved_root, "cache"),
        "PYTHONPYCACHEPREFIX": os.path.join(resolved_root, "cache", "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUFF_CACHE_DIR": os.path.join(resolved_root, "cache", "ruff"),
        "COVERAGE_FILE": os.path.join(resolved_root, "tmp", "coverage"),
        "PIP_CACHE_DIR": os.path.join(resolved_root, "cache", "pip"),
        "PYTEST_ADDOPTS": f"-o cache_dir={os.path.join(resolved_root, 'cache', 'pytest')}",
    }
    for key, value in expected.items():
        assert environment[key] == value, key

    non_validation = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.ENGINEER_BE, _request(), attested_head="0" * 40
    )
    assert non_validation.bound_root is None
    assert not (
        set(non_validation.environment) & usage_cli._VALIDATION_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    )


def test_validation_environment_never_touches_home_and_config_dir_still_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``HOME`` is untouched by the closed map; ``CLAUDE_CONFIG_DIR`` still rejects outright."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    monkeypatch.setenv("HOME", "/original/home")
    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.QA, _request(validation_root=str(root)), attested_head="0" * 40
    )
    assert launch_environment.environment.get("HOME") == "/original/home"

    _configure_native_launcher(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "SENTINEL_CONFIG")
    with pytest.raises(SystemExit, match="config directory is not permitted") as error:
        main(_native_cli_args(role="qa", validation_root=str(root)))
    assert "SENTINEL_CONFIG" not in str(error.value)


def test_validation_root_rejects_relative_and_traversal_paths() -> None:
    """Relative, traversing, double-slash, and trailing-slash paths all fail closed."""
    for candidate in ("relative/path", "/abs/../escape", "/abs//double", "/abs/trailing/"):
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            usage_cli._validate_validation_root(candidate)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "forbidden",
    [
        "/abs/has space",
        "/abs/has\ttab",
        "/abs/has\nnewline",
        "/abs/has\x7fdel",
        "/abs/has'quote",
        '/abs/has"quote',
        "/abs/has\\backslash",
    ],
)
def test_validation_path_predicate_rejects_forbidden_characters(forbidden: str) -> None:
    """D167's shared grammar predicate rejects whitespace, control, quote, and backslash."""
    assert not usage_cli._is_valid_bound_root_path(forbidden)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(forbidden)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "forbidden",
    [
        "/abs/has)paren",
        "/abs/has(paren",
        "/abs/has:colon",
        "/abs/has*star",
        "/abs/has,comma",
        "/abs/has;semi",
        "/abs/has&amp",
        "/abs/has|pipe",
        "/abs/has$dollar",
        "/abs/has`tick",
        "/abs/has<lt",
        "/abs/has>gt",
        "/abs/has!bang",
        "/abs/has?question",
        "/abs/has[bracket",
        "/abs/has]bracket",
        "/abs/has{brace",
        "/abs/has}brace",
        "/abs/has#hash",
        "/abs/has~tilde",
    ],
)
def test_validation_path_predicate_rejects_rule_grammar_and_shell_metacharacters(
    forbidden: str,
) -> None:
    """T12: the closed positive grammar rejects every rule/shell metacharacter.

    A rendered rule is ``Bash(<command containing this path>)``; any of these
    characters surviving inside the path could re-scope or truncate that rule
    (§2.5), so the closed positive segment grammar must reject every one of
    them even though the D167 negative predicate did not.
    """
    assert not usage_cli._is_valid_bound_root_path(forbidden)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(forbidden)  # pyright: ignore[reportPrivateUsage]


def test_validation_path_predicate_rejects_reserved_dot_segments() -> None:
    """T12: a bare ``.`` or ``..`` segment is rejected even though both match the charset."""
    assert not usage_cli._is_valid_bound_root_path("/abs/./root")  # pyright: ignore[reportPrivateUsage]
    assert not usage_cli._is_valid_bound_root_path("/abs/../root")  # pyright: ignore[reportPrivateUsage]


def test_validation_root_resolved_form_with_rule_metacharacter_renders_no_rule(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T12a: a resolved root containing ``)`` is rejected before any rule is rendered.

    The forbidden character sits on an ancestor reached only through a clean
    symlink (mirroring the existing resolved-form test above), proving the
    rule grammar cannot be escaped by routing a rule-breaking character
    through the resolved path rather than the raw one.
    """
    base = tmp_path_factory.mktemp("resolved-paren")
    forbidden_ancestor = base / "has)paren"
    (forbidden_ancestor / "nested").mkdir(parents=True)
    actual_root = _build_validation_root(forbidden_ancestor / "nested" / "root")
    link = base / "clean-link"
    link.symlink_to(forbidden_ancestor)
    raw = str(link / "nested" / "root")
    assert usage_cli._is_valid_bound_root_path(raw)  # pyright: ignore[reportPrivateUsage]
    resolved = os.path.realpath(raw)
    assert resolved == os.path.realpath(actual_root)
    assert not usage_cli._is_valid_bound_root_path(resolved)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(raw)  # pyright: ignore[reportPrivateUsage]
    # The single upstream validation (proven above) is the only gate: the full
    # pipeline call that would otherwise feed a validated root into argv
    # construction fails here too, so no rule is ever rendered from the
    # rejected resolved path.
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
            NativeClaudeRole.QA, _request(validation_root=raw), attested_head="0" * 40
        )


def test_validation_path_predicate_applies_to_raw_and_resolved_forms(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A clean raw path resolving through an ancestor symlink to a forbidden path fails closed.

    The symlink sits on an ANCESTOR of the root, not the root's own last path
    component: opening ``raw`` with ``O_NOFOLLOW`` only inspects the final
    component, which is a real (non-symlink) directory, so that open alone
    would succeed. Only the shared grammar predicate re-applied to the
    resolved path (D167) catches the forbidden character the ancestor symlink
    resolves through.
    """
    base = tmp_path_factory.mktemp("resolved-forbidden")
    forbidden_ancestor = base / "has'quote"
    (forbidden_ancestor / "nested").mkdir(parents=True)
    actual_root = _build_validation_root(forbidden_ancestor / "nested" / "root")
    link = base / "clean-link"
    link.symlink_to(forbidden_ancestor)
    raw = str(link / "nested" / "root")
    assert usage_cli._is_valid_bound_root_path(raw)  # pyright: ignore[reportPrivateUsage]
    resolved = os.path.realpath(raw)
    assert resolved == os.path.realpath(actual_root)
    assert not usage_cli._is_valid_bound_root_path(resolved)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(raw)  # pyright: ignore[reportPrivateUsage]


def test_validation_root_accepts_clean_intermediate_ancestor_symlink(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A clean intermediate-ancestor symlink is the documented accepted residual, not the root."""
    base = tmp_path_factory.mktemp("ancestor-symlink")
    actual = base / "actual"
    actual.mkdir()
    root = _build_validation_root(actual / "root")
    link = base / "link"
    link.symlink_to(actual)
    via_ancestor_symlink = link / "root"
    resolved = usage_cli._validate_validation_root(  # pyright: ignore[reportPrivateUsage]
        str(via_ancestor_symlink)
    )
    assert resolved == os.path.realpath(root)


def test_validation_root_rejects_symlinked_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A symlinked root component fails closed even when its target is otherwise valid."""
    base = tmp_path_factory.mktemp("symlink-root")
    real_root = _build_validation_root(base / "real")
    linked = base / "linked"
    linked.symlink_to(real_root)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(linked))  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("component", ["root", "cache", "tmp"])
def test_validation_root_rejects_wrong_directory_mode(
    component: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The root, ``cache``, and ``tmp`` must each be exactly mode 0700."""
    base = tmp_path_factory.mktemp(f"wrong-mode-{component}")
    root = _build_validation_root(base / "root")
    target = root if component == "root" else root / component
    os.chmod(target, 0o755)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]


def test_validation_root_rejects_foreign_uid(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory not owned by the current effective user fails closed."""
    base = tmp_path_factory.mktemp("foreign-uid")
    root = _build_validation_root(base / "root")
    real_fstat = os.fstat

    def foreign_fstat(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        fields = list(status)
        fields[4] = status.st_uid + 1  # st_uid index
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_fstat)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]


def test_validation_root_rejects_realpath_resolution_failure(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A realpath resolution failure for root, cwd, or home fails closed, never raising raw."""
    base = tmp_path_factory.mktemp("realpath-failure")
    root = _build_validation_root(base / "root")
    real_realpath = os.path.realpath

    def failing_realpath(path: str) -> str:
        if path == str(root):
            raise OSError("SENTINEL_REALPATH_FAILURE")
        return real_realpath(path)

    monkeypatch.setattr(os.path, "realpath", failing_realpath)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid") as error:
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]
    assert "SENTINEL_REALPATH_FAILURE" not in str(error.value)


def test_validation_root_venv_mode_is_unconstrained(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``venv``'s mode is deliberately unconstrained; the root's 0700 is the boundary."""
    base = tmp_path_factory.mktemp("venv-mode")
    root = _build_validation_root(base / "root")
    os.chmod(root / "venv", 0o755)
    resolved = usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]
    assert resolved == os.path.realpath(root)


@pytest.mark.parametrize(
    "mutate", ["missing-venv", "missing-cfg", "hardlinked-cfg", "non-exec-interpreter"]
)
def test_validation_root_rejects_malformed_venv_shape(
    mutate: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A missing venv, missing marker, hardlinked marker, or non-exec interpreter fails closed."""
    base = tmp_path_factory.mktemp(f"venv-shape-{mutate}")
    root = _build_validation_root(base / "root")
    if mutate == "missing-venv":
        shutil.rmtree(root / "venv")
    elif mutate == "missing-cfg":
        (root / "venv" / "pyvenv.cfg").unlink()
    elif mutate == "hardlinked-cfg":
        (root / "venv" / "pyvenv.cfg").unlink()
        target = base / "hardlink-target.cfg"
        target.write_text("home = /usr\n")
        os.link(target, root / "venv" / "pyvenv.cfg")
    else:
        (root / "venv" / "bin" / "python").chmod(0o644)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]


def test_validation_root_accepts_symlinked_interpreter(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A ``venv/bin/python`` symlink to the base interpreter is the practical case, and passes."""
    base = tmp_path_factory.mktemp("symlink-interp")
    root = _build_validation_root(base / "root")
    interpreter = root / "venv" / "bin" / "python"
    interpreter.unlink()
    real = base / "real-python"
    real.write_bytes(b"#!/bin/sh\necho fake\n")
    real.chmod(0o755)
    interpreter.symlink_to(real)
    resolved = usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]
    assert resolved == os.path.realpath(root)


def test_validation_root_missing_is_rejected_and_not_created(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A nonexistent root is rejected, never implicitly created."""
    base = tmp_path_factory.mktemp("missing-root")
    missing = base / "does-not-exist"
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(missing))  # pyright: ignore[reportPrivateUsage]
    assert not missing.exists()


def test_validation_root_rejects_worktree_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A root equal to, inside, or containing the attested worktree fails closed both ways."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(worktree))  # pyright: ignore[reportPrivateUsage]

    inside = _build_validation_root(worktree / "nested-root")
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(inside))  # pyright: ignore[reportPrivateUsage]

    outer = tmp_path_factory.mktemp("outer-root")
    root = _build_validation_root(outer / "root")
    nested_worktree = root / "nested-worktree"
    nested_worktree.mkdir()
    monkeypatch.chdir(nested_worktree)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]


def test_validation_root_rejects_inside_claude_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root located inside ``~/.claude`` fails closed."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    inside_claude = _build_validation_root(home / ".claude" / "root")
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._validate_validation_root(str(inside_claude))  # pyright: ignore[reportPrivateUsage]


def test_native_launch_validates_root_exactly_once_and_binds_add_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """One root validation supplies both the env root and the argv ``--add-dir`` root."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_read_only_transcript_bytes("qa")),
    )
    real_validate = usage_cli._validate_validation_root  # pyright: ignore[reportPrivateUsage]
    calls: list[str] = []

    def counting_validate(raw: str) -> str:
        calls.append(raw)
        return real_validate(raw)

    monkeypatch.setattr(usage_cli, "_validate_validation_root", counting_validate)
    assert main(_native_cli_args(role="qa", validation_root=str(root))) == 0
    assert calls == [str(root)]

    worker_argv = observed[1][0]
    resolved_root = os.path.realpath(root)
    add_dir_index = worker_argv.index("--add-dir")
    assert worker_argv[add_dir_index + 1] == resolved_root
    assert worker_argv[add_dir_index + 2] == "--permission-mode"


def test_native_launch_allowed_tools_rules_use_the_resolved_root_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T5 (full pipeline): a symlinked ``--validation-root`` never leaks into rendered rules.

    Complements the direct-call T5 test: this one exercises the real
    ``run_native_claude_command`` call site, proving the caller passes the
    validated *resolved* root into :func:`_native_claude_argv`, not the raw
    ``--validation-root`` argument (M12).
    """
    base = tmp_path_factory.mktemp("e2e-symlink-root")
    actual = base / "actual"
    actual.mkdir()
    root = _build_validation_root(actual / "root")
    link = base / "link"
    link.symlink_to(actual)
    raw_root = link / "root"

    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_read_only_transcript_bytes("qa")),
    )
    assert main(_native_cli_args(role="qa", validation_root=str(raw_root))) == 0

    worker_argv = observed[1][0]
    resolved_root = os.path.realpath(root)
    rules = worker_argv[worker_argv.index("--allowedTools") + 1 :]
    assert rules
    assert all(str(link) not in rule for rule in rules)
    assert any(resolved_root in rule for rule in rules)


def test_native_launch_add_dir_absent_for_write_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WRITE (non-validation) native launch's argv never carries ``--add-dir``."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_native_transcript_bytes()),
    )
    assert main(_native_cli_args(role="engineer-be")) == 0
    worker_argv = observed[1][0]
    assert "--add-dir" not in worker_argv


def test_validation_environment_key_set_has_exactly_eleven_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed key set is exactly eleven names; a WRITE launch strips every one."""
    keys = usage_cli._VALIDATION_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    assert len(keys) == 11
    for key in keys:
        monkeypatch.setenv(key, "SENTINEL_PRESET")
    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.ENGINEER_BE, _request(), attested_head="0" * 40
    )
    assert launch_environment.bound_root is None
    assert not (set(launch_environment.environment) & keys)


def test_validation_root_is_never_mutated_by_capture_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A successful validation-role run leaves the root's listing and mtimes untouched."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    before = {str(path.relative_to(root)): path.stat().st_mtime_ns for path in root.rglob("*")}
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_read_only_transcript_bytes("qa")),
    )
    assert main(_native_cli_args(role="qa", validation_root=str(root))) == 0
    after = {str(path.relative_to(root)): path.stat().st_mtime_ns for path in root.rglob("*")}
    assert before == after


def test_validation_root_path_never_leaks_into_errors_or_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The root path never appears in an error, stdout metadata field, or record."""
    sentinel_root = tmp_path_factory.mktemp("SENTINELROOTCOMPONENT")
    root = _build_validation_root(sentinel_root / "root")
    os.chmod(root, 0o755)
    with pytest.raises(CaptureUsageError) as error:
        usage_cli._validate_validation_root(str(root))  # pyright: ignore[reportPrivateUsage]
    assert "SENTINELROOTCOMPONENT" not in str(error.value)

    os.chmod(root, 0o700)
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes("qa", text="clean handback, no path"),
        ),
    )
    assert main(_native_cli_args(role="qa", validation_root=str(root))) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.splitlines()[0])
    for key, value in payload.items():
        if key == "text":
            continue
        assert "SENTINELROOTCOMPONENT" not in str(value)
    raw_sink = Path(".agent-usage/usage.jsonl").read_text()
    assert "SENTINELROOTCOMPONENT" not in raw_sink
    assert "SENTINELROOTCOMPONENT" not in captured.err


def test_validation_role_ignored_artifact_still_fails_post_exit_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A run dirtying the worktree with an ignored artifact still fails closed (no allowlist)."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    before_listing = set(tmp_path.rglob("*"))

    def snapshot_attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        if post_exit and set(tmp_path.rglob("*")) != before_listing:
            raise CaptureUsageError("native worktree attestation failed")
        return "4c1ac63"

    monkeypatch.setattr(usage_cli, "_validate_native_worktree", snapshot_attestation)
    processes: list[_NativeProcess] = []

    def dirtying_popen(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            process = _NativeProcess(b"Claude Code 2.1.233\n")
            processes.append(process)
            return process
        (project / f"{_NATIVE_SESSION_ID}.jsonl").write_bytes(_read_only_transcript_bytes("qa"))
        (tmp_path / ".pytest_cache").mkdir()
        process = _NativeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(usage_cli.subprocess, "Popen", dirtying_popen)
    with pytest.raises(SystemExit, match="native worktree attestation failed"):
        main(_native_cli_args(role="qa", validation_root=str(root)))
    assert not Path(".agent-usage/usage.jsonl").exists()


def test_validation_environment_roles_are_read_only_bash_only_subset() -> None:
    """The three validation roles are READ_ONLY, declare Bash, and never Edit/Write."""
    assert {
        role
        for role in NativeClaudeRole
        if usage_cli._native_role_pin(role).capability  # pyright: ignore[reportPrivateUsage]
        is RoleCapability.READ_ONLY
    } >= usage_cli.VALIDATION_ENVIRONMENT_ROLES
    for role in usage_cli.VALIDATION_ENVIRONMENT_ROLES:
        path = Path(".claude") / "agents" / f"{role.value}.md"
        tools_line = next(
            line for line in path.read_text().splitlines() if line.startswith("tools: ")
        )
        tools = {token.strip() for token in tools_line.removeprefix("tools: ").split(",")}
        assert "Bash" in tools
        assert not ({"Edit", "Write"} & tools)


def test_provisioning_docs_set_bytecode_variables_on_both_pip_commands() -> None:
    """Both provisioning ``pip install`` recipe lines set both bytecode-containment vars."""
    doc = Path(__file__).resolve().parents[1] / "docs" / "agent-team-worktrees.md"
    lines = doc.read_text().splitlines()
    pip_lines = [line for line in lines if 'venv/bin/python" -m pip install' in line]
    assert len(pip_lines) == 2
    for line in pip_lines:
        assert "PYTHONPYCACHEPREFIX=" in line
        assert "PYTHONDONTWRITEBYTECODE=1" in line


# ---------------------------------------------------------------------------
# D168 round-9 repair: committed per-role validation --allowedTools allow-list,
# the closed positive path grammar, and the single-source-of-truth command
# renderer shared by the argv builder and ``print-validation-commands``.
# ---------------------------------------------------------------------------

_VALIDATION_ROLE_FILES = (
    Path(".claude") / "agents" / "qa.md",
    Path(".claude") / "agents" / "mcp-contract-checker.md",
    Path(".claude") / "agents" / "sim-roast-runner.md",
)


def test_print_validation_commands_matches_the_argv_rule_table(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T14: ``print-validation-commands`` output equals the table wrapped into ``--allowedTools``.

    Proves the single source of truth: the printed lines and the argv rules
    can never diverge because both are rendered from
    :data:`VALIDATION_ROLE_COMMANDS` through the same
    :func:`render_validation_commands` call.
    """
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    for role in usage_cli.VALIDATION_ENVIRONMENT_ROLES:
        exit_code = main(
            ["print-validation-commands", "--role", role.value, "--validation-root", str(root)]
        )
        assert exit_code == 0
        printed = capsys.readouterr().out.splitlines()
        expected_rendered = render_validation_commands(role, resolved_root)
        allow_lines = [
            f"ALLOW {command.kind.value} {text}"
            for command, text in zip(VALIDATION_ROLE_COMMANDS[role], expected_rendered, strict=True)
        ]
        run_lines = [f"RUN {text}" for text in expected_rendered]
        assert printed == [*allow_lines, *run_lines]
        assert printed.index(run_lines[0]) == len(allow_lines)

        rules = render_allowed_tools(role, resolved_root)
        stripped: list[str] = []
        for command, rule in zip(VALIDATION_ROLE_COMMANDS[role], rules, strict=True):
            inner = rule.removeprefix("Bash(").removesuffix(")")
            if command.kind is ValidationCommandKind.PREFIX:
                assert inner.endswith(":*")
                inner = inner.removesuffix(":*")
            stripped.append(inner)
        assert tuple(stripped) == expected_rendered


def test_print_validation_commands_rejects_non_validation_role_before_output(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T14: a non-validation role fails closed with no partial stdout."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                "engineer-be",
                "--validation-root",
                str(root),
            ]
        )
    assert capsys.readouterr().out == ""


def test_print_validation_commands_rejects_invalid_root_before_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T14: an invalid root fails closed with no partial stdout."""
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                "relative/path",
            ]
        )
    assert capsys.readouterr().out == ""


def test_print_validation_commands_rejects_empty_rendered_tuple_before_output(
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14: an empty rendered tuple for a validation role fails closed, no partial stdout.

    Defensive symmetry with the argv builder's own empty-tuple guard (T11):
    this is the same backstop reachable only if the shared render function
    were ever mutated to return nothing for a role the table still lists.
    """
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")

    def empty_render(_role: NativeClaudeRole, _root: str) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(usage_cli, "render_validation_commands", empty_render)
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                str(root),
            ]
        )
    assert capsys.readouterr().out == ""


def test_no_caller_surface_on_print_validation_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``print-validation-commands`` exposes no rule, tool, model, effort, or mode override."""
    for flag in ("--allowed-tools", "--allowedTools", "--tools", "--permission-mode", "--model"):
        with pytest.raises(SystemExit):
            usage_cli.build_parser().parse_args(
                [
                    "print-validation-commands",
                    "--role",
                    "qa",
                    "--validation-root",
                    "/validated/root",
                    flag,
                    "value",
                ]
            )
        assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


def test_validation_role_files_never_use_command_position_static_paths() -> None:
    """T15: no role file names a static command-position interpreter or env-var path.

    The round-8 live failure proved ``$ROASTPILOT_VALIDATION_*`` command text
    does not match an absolute-path provider allow rule under ``dontAsk``; the
    fix is that every role file requires the parent-supplied exact
    ``print-validation-commands`` output instead. The D154 routed-control
    ``## Worktree discipline`` block is shared byte-for-byte across every
    READ_ONLY role (``tests/test_agent_worktree_controls.py``) and is
    intentionally left unedited here; its generic, non-runnable
    ``.venv/bin/python -m …`` prose is explicitly superseded, within this
    role's own Validation environment section, for these three roles.
    """
    forbidden_command_fragments = (
        '"$ROASTPILOT_VALIDATION_PYTHON" -m',
        "$ROASTPILOT_VALIDATION_PYTHON -m",
        '"$ROASTPILOT_VALIDATION_TMP"',
        "`python -m",
    )
    for path in _VALIDATION_ROLE_FILES:
        text = path.read_text()
        for fragment in forbidden_command_fragments:
            assert fragment not in text, (path, fragment)
        assert "print-validation-commands" in text
        assert "governs" in text and "write-capable workers only" in text


def test_validation_role_files_never_instruct_python_c_pip_install_or_worktree_venv() -> None:
    """T16: no role file contains an interpreter one-liner, pip install, or venv creation."""
    for path in _VALIDATION_ROLE_FILES:
        text = path.read_text()
        assert "python -c" not in text
        assert "pip install" not in text
        assert "-m venv" not in text


def test_native_safety_and_security_reviewers_are_evidence_only() -> None:
    """Option A keeps mandatory assurance review free of native shell authority."""
    agents = Path(__file__).resolve().parents[1] / ".claude" / "agents"
    for name in ("safety-reviewer", "security-reviewer"):
        text = (agents / f"{name}.md").read_text()
        assert "tools: Read, Grep, Glob\n" in text
        assert "tools: Read, Grep, Glob, Bash" not in text
        assert "Parent-supplied review evidence" in text
        assert "Do not run shell commands" in text
    assert (
        "python -m pytest tests/test_controller.py tests/test_safety.py -q"
        not in (agents / "safety-reviewer.md").read_text()
    )
    assert "git diff origin/main...HEAD" not in (agents / "security-reviewer.md").read_text()


def test_runbook_and_skill_and_agents_row_point_to_print_validation_commands() -> None:
    """T17: the runbook, SKILL.md, and the AGENTS.md row cite the single interface.

    None of the three duplicates the seven dynamic per-role command strings —
    only ``print-validation-commands`` (run by the parent) can render them,
    because they depend on the per-run root.
    """
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs" / "agent-team-worktrees.md").read_text()
    skill = (root / ".agents" / "skills" / "capture-agent-usage" / "SKILL.md").read_text()
    agents = (root / "AGENTS.md").read_text()
    for text in (runbook, skill, agents):
        assert "print-validation-commands" in text
        assert "dontAsk" in text or "denied" in text
    assert "D168" in runbook
    assert "D168" in skill
    assert "D168" in agents
    # None of the three documents hardcodes a rendered command against a
    # concrete filesystem root (that would duplicate the per-run table);
    # the runbook's own bash provisioning recipe legitimately uses a real
    # `venv/bin/python` invocation to build the environment, which is
    # distinct from a rendered gate command.
    for text in (skill, agents):
        assert "tests/test_mcp_client.py" not in text
        assert "tests/test_milestone1.py" not in text


def test_validation_environment_roles_include_only_bash_capable_read_only_roles() -> None:
    """T8: table membership matches the committed frontmatter capability, both directions."""
    for role in NativeClaudeRole:
        pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
        in_table = role in VALIDATION_ROLE_COMMANDS
        if role in usage_cli.VALIDATION_ENVIRONMENT_ROLES:
            assert in_table
            assert pin.capability is RoleCapability.READ_ONLY
        else:
            assert not in_table


# ---------------------------------------------------------------------------
# D169 round-10 repair: generalized bound-root abstraction (VALIDATION / PLAN /
# EVIDENCE), plan-root and evidence-bundle validation, and the
# print-validation-commands ALLOW/RUN grammar.
# ---------------------------------------------------------------------------

_PLAN_REQUIRED_ROLES = {NativeClaudeRole.PLANNING_ARCHITECT, NativeClaudeRole.STORY_PLANNER}
_PLAN_OPTIONAL_ROLES = {NativeClaudeRole.PRODUCT_AUDITOR}
_EVIDENCE_REQUIRED_ROLES = {NativeClaudeRole.PR_TRIAGE}


def test_bound_root_policy_closure_invariants() -> None:
    """T3: required/optional never overlap per policy; the three admitted sets are disjoint."""
    assert set(BOUND_ROOT_POLICIES) == {
        BoundRootKind.VALIDATION,
        BoundRootKind.PLAN,
        BoundRootKind.EVIDENCE,
    }
    admitted_sets: list[frozenset[NativeClaudeRole]] = []
    for policy in BOUND_ROOT_POLICIES.values():
        assert not (policy.required_roles & policy.optional_roles)
        admitted_sets.append(policy.required_roles | policy.optional_roles)
    for first, second in itertools.combinations(admitted_sets, 2):
        assert not (first & second)
    validation_policy = BOUND_ROOT_POLICIES[BoundRootKind.VALIDATION]
    assert validation_policy.required_roles is VALIDATION_ENVIRONMENT_ROLES
    assert validation_policy.optional_roles == frozenset()
    plan_policy = BOUND_ROOT_POLICIES[BoundRootKind.PLAN]
    assert plan_policy.required_roles == frozenset(_PLAN_REQUIRED_ROLES)
    assert plan_policy.optional_roles == frozenset(_PLAN_OPTIONAL_ROLES)
    evidence_policy = BOUND_ROOT_POLICIES[BoundRootKind.EVIDENCE]
    assert evidence_policy.required_roles == frozenset(_EVIDENCE_REQUIRED_ROLES)
    assert evidence_policy.optional_roles == frozenset()
    for role in NativeClaudeRole:
        assert isinstance(role, NativeClaudeRole)


def test_bound_root_admitted_roles_derive_read_only_from_frontmatter() -> None:
    """T4: every PLAN/EVIDENCE-admitted role is READ_ONLY per committed frontmatter."""
    for role in (*_PLAN_REQUIRED_ROLES, *_PLAN_OPTIONAL_ROLES, *_EVIDENCE_REQUIRED_ROLES):
        pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
        assert pin.capability is RoleCapability.READ_ONLY


@pytest.mark.parametrize("role", list(NativeClaudeRole))
def test_bound_root_presence_matrix(
    role: NativeClaudeRole, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T1: presence is required for exactly the required roles, forbidden for non-admitted ones."""
    validation_root = str(_build_validation_root(tmp_path_factory.mktemp("base") / "root"))
    plan_root, plan_sha = "/plan/root", "1" * 40
    evidence_root, evidence_pr = "/evidence/root", 1

    def presence(
        *,
        validation_root: str | None = None,
        plan_root: str | None = None,
        plan_sha: str | None = None,
        evidence_root: str | None = None,
        evidence_pr: int | None = None,
    ) -> BoundRootKind | None:
        return usage_cli._bound_root_presence(  # pyright: ignore[reportPrivateUsage]
            role,
            _request(
                validation_root=validation_root,
                plan_root=plan_root,
                plan_sha=plan_sha,
                evidence_root=evidence_root,
                evidence_pr=evidence_pr,
            ),
        )

    if role in VALIDATION_ENVIRONMENT_ROLES:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence()
        assert presence(validation_root=validation_root) is BoundRootKind.VALIDATION
    else:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence(validation_root=validation_root)

    if role in _PLAN_REQUIRED_ROLES:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence()
        assert presence(plan_root=plan_root, plan_sha=plan_sha) is BoundRootKind.PLAN
    elif role in _PLAN_OPTIONAL_ROLES:
        assert presence() is None
        assert presence(plan_root=plan_root, plan_sha=plan_sha) is BoundRootKind.PLAN
    else:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence(plan_root=plan_root, plan_sha=plan_sha)

    if role in _EVIDENCE_REQUIRED_ROLES:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence()
        assert (
            presence(evidence_root=evidence_root, evidence_pr=evidence_pr) is BoundRootKind.EVIDENCE
        )
    else:
        with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
            presence(evidence_root=evidence_root, evidence_pr=evidence_pr)

    if role not in (
        VALIDATION_ENVIRONMENT_ROLES
        | _PLAN_REQUIRED_ROLES
        | _PLAN_OPTIONAL_ROLES
        | _EVIDENCE_REQUIRED_ROLES
    ):
        assert presence() is None


@pytest.mark.parametrize(
    "role", [NativeClaudeRole.STORY_PLANNER, NativeClaudeRole.PRODUCT_AUDITOR, NativeClaudeRole.QA]
)
def test_bound_root_presence_rejects_root_without_companion(role: NativeClaudeRole) -> None:
    """T2: a root without its required companion (or vice versa) rejects for every role."""

    def presence(
        *,
        plan_root: str | None = None,
        plan_sha: str | None = None,
        evidence_root: str | None = None,
        evidence_pr: int | None = None,
    ) -> BoundRootKind | None:
        return usage_cli._bound_root_presence(  # pyright: ignore[reportPrivateUsage]
            role,
            _request(
                plan_root=plan_root,
                plan_sha=plan_sha,
                evidence_root=evidence_root,
                evidence_pr=evidence_pr,
            ),
        )

    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        presence(plan_root="/plan/root")
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        presence(plan_sha="1" * 40)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        presence(evidence_root="/evidence/root")
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        presence(evidence_pr=1)


def test_bound_root_presence_runs_before_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5: a missing required plan-root pair never reaches ``shutil.which``."""
    _configure_native_launcher(tmp_path, monkeypatch)

    def no_which(_name: str) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(usage_cli.shutil, "which", no_which)
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(_native_cli_args(role="story-planner"))


def test_bound_root_at_most_one_admitted_per_role() -> None:
    """Consequence of closure (§2.2): every role is admitted by at most one policy."""
    for role in NativeClaudeRole:
        admitting = [
            kind
            for kind, policy in BOUND_ROOT_POLICIES.items()
            if role in policy.required_roles or role in policy.optional_roles
        ]
        assert len(admitting) <= 1


# --- PLAN kind -------------------------------------------------------------

_PLAN_ORIGIN = "https://github.com/syamaner/roastpilot-plan.git"


def _build_plan_root(root: Path) -> Path:
    """Build one fully valid parent-provisioned plan-root shape at ``root`` (D169, §2.3)."""
    _mkdir_exact(root, 0o700)
    return root


def _fixed_plan_git(
    root: str,
    sha: str,
    *,
    toplevel: str | None = None,
    origin: str = _PLAN_ORIGIN,
    head: str | None = None,
    verify_status: int = 0,
    status: str = "",
) -> Callable[[list[str]], tuple[int, str]]:
    """Return a deterministic ``_git_output`` double for one plan-root identity sequence."""
    toplevel = root if toplevel is None else toplevel
    head = sha if head is None else head

    def fake_git(argv: list[str]) -> tuple[int, str]:
        assert argv[:2] == ["-C", root], argv
        rest = argv[2:]
        if rest == ["rev-parse", "--show-toplevel"]:
            return 0, toplevel
        if rest == ["remote", "get-url", "origin"]:
            return 0, origin
        if rest == ["rev-parse", "HEAD"]:
            return 0, head
        if rest == ["rev-parse", "--verify", f"{sha}^{{commit}}"]:
            return (verify_status, sha if verify_status == 0 else "")
        if rest == ["status", "--porcelain", "--ignored"]:
            return 0, status
        raise AssertionError(argv)

    return fake_git


def test_plan_root_binds_add_dir_and_no_allowed_tools(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: a valid plan root binds; one ``--add-dir`` immediately before ``--permission-mode``."""
    root = _build_plan_root(tmp_path_factory.mktemp("plan-base") / "root")
    sha = "1" * 40
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(str(root), sha))
    bound = usage_cli._validate_plan_root(str(root), sha)  # pyright: ignore[reportPrivateUsage]
    assert bound.kind is BoundRootKind.PLAN
    assert bound.path == os.path.realpath(root)
    assert bound.reattest is not None
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.STORY_PLANNER,
        RoleCapability.READ_ONLY,
        "high",
        "11111111-1111-4111-8111-111111111233",
        bound,
    )
    assert argv[argv.index("--add-dir") + 1] == bound.path
    assert argv[argv.index("--add-dir") + 2] == "--permission-mode"
    assert "--allowedTools" not in argv


def test_plan_environment_binds_exact_key_and_strips_evidence_and_validation_keys(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PLAN-kind launch binds exactly ``ROASTPILOT_PLAN_ROOT`` and strips the rest.

    Exercises the actual :func:`usage_cli._resolve_native_environment` result
    (not the constants alone), including a *stale* inherited
    ``ROASTPILOT_PLAN_ROOT`` value that must be overwritten by the freshly
    resolved root, and a stale ``ROASTPILOT_EVIDENCE_ROOT`` plus every
    validation key that must be stripped outright.
    """
    root = _build_plan_root(tmp_path_factory.mktemp("plan-base") / "root")
    sha = "2" * 40
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(str(root), sha))
    monkeypatch.setenv(PLAN_ROOT_ENVIRONMENT_KEY, "STALE_PLAN_SENTINEL")
    monkeypatch.setenv(EVIDENCE_ROOT_ENVIRONMENT_KEY, "STALE_EVIDENCE_SENTINEL")
    for key in usage_cli._VALIDATION_ENVIRONMENT_KEYS:  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv(key, "STALE_VALIDATION_SENTINEL")

    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.STORY_PLANNER,
        _request(plan_root=str(root), plan_sha=sha),
        attested_head="0" * 40,
    )
    resolved_root = os.path.realpath(root)
    assert launch_environment.bound_root is not None
    assert launch_environment.bound_root.kind is BoundRootKind.PLAN
    assert launch_environment.bound_root.path == resolved_root

    environment = launch_environment.environment
    assert environment[PLAN_ROOT_ENVIRONMENT_KEY] == resolved_root
    assert EVIDENCE_ROOT_ENVIRONMENT_KEY not in environment
    assert not (
        set(environment) & usage_cli._VALIDATION_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    )

    # A role with no admitting policy sees the key stripped too, including the
    # stale value this test just set above.
    other_role_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.ENGINEER_BE, _request(), attested_head="0" * 40
    )
    assert other_role_environment.bound_root is None
    assert PLAN_ROOT_ENVIRONMENT_KEY not in other_role_environment.environment
    assert EVIDENCE_ROOT_ENVIRONMENT_KEY not in other_role_environment.environment


@pytest.mark.parametrize(
    "mutate",
    [
        "wrong-origin",
        "sha-not-head",
        "non-40-hex-sha",
        "dirty-tracked",
        "untracked",
        "ignored",
        "toplevel-mismatch",
        "root-mode",
        "foreign-uid",
        "symlinked-root",
        "root-inside-cwd",
        "cwd-inside-root",
        "root-inside-claude-home",
        "root-contains-claude-home",
        "git-unavailable",
    ],
)
def test_plan_root_rejects_every_identity_break(
    mutate: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """T7: every plan-root identity, shape, or cleanliness break fails closed."""
    sha = "1" * 40
    if mutate in (
        "root-inside-cwd",
        "cwd-inside-root",
        "root-inside-claude-home",
        "root-contains-claude-home",
    ):
        monkeypatch.chdir(tmp_path)
    base = tmp_path_factory.mktemp(f"plan-reject-{mutate}")
    root = _build_plan_root(base / "root")
    if mutate == "root-mode":
        os.chmod(root, 0o755)
    if mutate == "foreign-uid":
        real_fstat = os.fstat

        def foreign_fstat(descriptor: int) -> os.stat_result:
            status = real_fstat(descriptor)
            fields = list(status)
            fields[4] = status.st_uid + 1
            return os.stat_result(fields)

        monkeypatch.setattr(os, "fstat", foreign_fstat)
    if mutate == "symlinked-root":
        linked = base / "linked"
        linked.symlink_to(root)
        root = linked
    if mutate == "root-inside-cwd":
        root = _build_plan_root(tmp_path / "nested-root")
    if mutate == "cwd-inside-root":
        monkeypatch.chdir(_mkdir_and_return(root / "nested-cwd"))
    if mutate == "root-inside-claude-home":
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
        root = _build_plan_root(home / ".claude" / "plan-root")
    if mutate == "root-contains-claude-home":
        home = root / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)

    origin = (
        "https://github.com/syamaner/roastpilot-agent.git"
        if mutate == "wrong-origin"
        else _PLAN_ORIGIN
    )
    head = "2" * 40 if mutate == "sha-not-head" else None
    resolved_root = os.path.realpath(root)
    toplevel = str(base / "somewhere-else") if mutate == "toplevel-mismatch" else None
    status = {
        "dirty-tracked": " M tracked-file.md",
        "untracked": "?? new-file.md",
        "ignored": "!! .venv/",
    }.get(mutate, "")

    if mutate == "git-unavailable":

        def unavailable(_argv: list[str]) -> tuple[int, str]:
            raise CaptureUsageError("Git worktree metadata is unavailable")

        monkeypatch.setattr(usage_cli, "_git_output", unavailable)
    else:
        monkeypatch.setattr(
            usage_cli,
            "_git_output",
            _fixed_plan_git(
                resolved_root, sha, toplevel=toplevel, origin=origin, head=head, status=status
            ),
        )

    use_sha = "abc123" if mutate == "non-40-hex-sha" else sha
    with pytest.raises(CaptureUsageError, match="plan root is invalid"):
        usage_cli._validate_plan_root(str(root), use_sha)  # pyright: ignore[reportPrivateUsage]


def _mkdir_and_return(path: Path) -> Path:
    """Create and return a directory, for inline chained construction."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_plan_root_rejects_missing_or_none_sha() -> None:
    """The plan-sha grammar rejects an absent companion at the deep-validation layer too."""
    with pytest.raises(CaptureUsageError, match="plan root is invalid"):
        usage_cli._validate_plan_root("/plan/root", None)  # pyright: ignore[reportPrivateUsage]


def test_plan_root_rejects_unresolvable_toplevel(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``git rev-parse --show-toplevel`` output that cannot be resolved fails closed.

    Calls :func:`_plan_identity_checks` directly (bypassing the earlier shape
    validation's own, unrelated ``realpath`` calls) to isolate this branch.
    """
    root = _build_plan_root(tmp_path_factory.mktemp("plan-bad-toplevel") / "root")
    sha = "1" * 40
    resolved = os.path.realpath(root)

    def failing_realpath(_path: str) -> str:
        raise OSError("SENTINEL_TOPLEVEL_REALPATH_FAILURE")

    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved, sha))
    monkeypatch.setattr(os.path, "realpath", failing_realpath)
    with pytest.raises(CaptureUsageError, match="plan root is invalid") as error:
        usage_cli._plan_identity_checks(resolved, sha)  # pyright: ignore[reportPrivateUsage]
    assert "SENTINEL_TOPLEVEL_REALPATH_FAILURE" not in str(error.value)


def test_plan_root_contents_modes_unconstrained(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8: plan-root file/directory modes inside the root are never constrained."""
    root = _build_plan_root(tmp_path_factory.mktemp("plan-contents") / "root")
    (root / "plan.md").write_text("content")
    os.chmod(root / "plan.md", 0o644)
    nested = root / "sub"
    nested.mkdir()
    os.chmod(nested, 0o755)
    sha = "1" * 40
    resolved = os.path.realpath(root)
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved, sha))
    bound = usage_cli._validate_plan_root(str(root), sha)  # pyright: ignore[reportPrivateUsage]
    assert bound.path == resolved


@pytest.mark.parametrize(
    "mutate", ["commit-landed", "file-dirtied", "ignored-file-appeared", "root-replaced"]
)
def test_plan_root_post_exit_drift_rejects(
    mutate: str, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9: any plan-root drift between launch and exit fails closed (M12)."""
    root = _build_plan_root(tmp_path_factory.mktemp("plan-post-exit") / "root")
    sha = "1" * 40
    resolved = os.path.realpath(root)
    calls = {"n": 0}
    dirty_status = {
        "commit-landed": "",
        "file-dirtied": " M tracked-file.md",
        "ignored-file-appeared": "!! .venv/",
        "root-replaced": "",
    }[mutate]
    dirty_head = "2" * 40 if mutate == "commit-landed" else sha

    def fake_git(argv: list[str]) -> tuple[int, str]:
        calls["n"] += 1
        if calls["n"] <= 5:
            return _fixed_plan_git(resolved, sha)(argv)
        return _fixed_plan_git(resolved, sha, head=dirty_head, status=dirty_status)(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    bound = usage_cli._validate_plan_root(str(root), sha)  # pyright: ignore[reportPrivateUsage]
    if mutate == "root-replaced":
        shutil.rmtree(root)
        _build_plan_root(root)
    assert bound.reattest is not None
    with pytest.raises(CaptureUsageError, match="plan root is invalid"):
        bound.reattest()


def test_plan_root_reattest_passes_when_untouched(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9 (negative control): an untouched plan root's reattest succeeds."""
    root = _build_plan_root(tmp_path_factory.mktemp("plan-clean-reattest") / "root")
    sha = "1" * 40
    resolved = os.path.realpath(root)
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved, sha))
    bound = usage_cli._validate_plan_root(str(root), sha)  # pyright: ignore[reportPrivateUsage]
    assert bound.reattest is not None
    bound.reattest()


def test_plan_root_never_mutated_by_capture_tool(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10: validating a plan root never writes to it."""
    root = _build_plan_root(tmp_path_factory.mktemp("plan-immutable") / "root")
    (root / "plan.md").write_text("content")
    before = {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*")}
    sha = "1" * 40
    resolved = os.path.realpath(root)
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved, sha))
    usage_cli._validate_plan_root(str(root), sha)  # pyright: ignore[reportPrivateUsage]
    after = {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*")}
    assert before == after


# --- EVIDENCE kind -----------------------------------------------------------

_EVIDENCE_HEAD = "a" * 40
_EVIDENCE_BASE = "b" * 40
_EVIDENCE_PAST_TIMESTAMP = "2025-01-01T00:00:00+00:00"


def _build_evidence_bundle(
    root: Path,
    *,
    pr: int = 837,
    head_sha: str = _EVIDENCE_HEAD,
    base_sha: str = _EVIDENCE_BASE,
    generated_at: str = _EVIDENCE_PAST_TIMESTAMP,
    payload_overrides: dict[str, bytes] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    files_overrides: dict[str, dict[str, object]] | None = None,
    file_mode: int = 0o400,
    skip_files: frozenset[str] = frozenset(),
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    """Build one fully valid parent-built PR evidence bundle at ``root`` (D169, §2.4)."""
    _mkdir_exact(root, 0o700)
    overrides = payload_overrides or {}
    files_entry: dict[str, object] = {}
    for name in EVIDENCE_PAYLOAD_FILES:
        if name in skip_files:
            continue
        data = overrides.get(
            name,
            (
                json.dumps({"number": pr, "headRefOid": head_sha, "baseRefOid": base_sha}).encode()
                if name == "pr.json"
                else f'{{"{name}": true}}'.encode()
            ),
        )
        target = root / name
        target.write_bytes(data)
        target.chmod(file_mode)
        files_entry[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if files_overrides:
        files_entry.update(files_overrides)
    manifest: dict[str, object] = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "repository": "syamaner/roastpilot-agent",
        "pull_request": pr,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "generated_at": generated_at,
        "files": files_entry,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(file_mode)
    if extra_files:
        for name, data in extra_files.items():
            (root / name).write_bytes(data)
            (root / name).chmod(file_mode)
    return root


def test_evidence_bundle_binds_add_dir_and_no_allowed_tools(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T11: a well-formed bundle binds; one ``--add-dir``, no ``--allowedTools``."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-happy") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.kind is BoundRootKind.EVIDENCE
    assert bound.path == os.path.realpath(root)
    assert bound.reattest is not None
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.PR_TRIAGE,
        RoleCapability.READ_ONLY,
        "high",
        "11111111-1111-4111-8111-111111111233",
        bound,
    )
    assert argv[argv.index("--add-dir") + 1] == bound.path
    assert argv[argv.index("--add-dir") + 2] == "--permission-mode"
    assert "--allowedTools" not in argv


@pytest.mark.parametrize(
    "payload",
    [
        b'{"number":838,"headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseRefOid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
        b'{"number":837,"headRefOid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","baseRefOid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
    ],
)
def test_evidence_bundle_rejects_pr_json_identity_mismatch(
    payload: bytes, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The retained offline PR payload must agree with manifest PR/head/base identity."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-pr-identity") / "root",
        payload_overrides={"pr.json": payload},
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_environment_binds_exact_key_and_strips_plan_and_validation_keys(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EVIDENCE-kind launch binds exactly ``ROASTPILOT_EVIDENCE_ROOT`` and strips the rest.

    Exercises the actual :func:`usage_cli._resolve_native_environment` result
    (not the constants alone), including a *stale* inherited
    ``ROASTPILOT_EVIDENCE_ROOT`` value that must be overwritten by the freshly
    resolved root, and a stale ``ROASTPILOT_PLAN_ROOT`` plus every validation
    key that must be stripped outright.
    """
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-env") / "root")
    monkeypatch.setenv(EVIDENCE_ROOT_ENVIRONMENT_KEY, "STALE_EVIDENCE_SENTINEL")
    monkeypatch.setenv(PLAN_ROOT_ENVIRONMENT_KEY, "STALE_PLAN_SENTINEL")
    for key in usage_cli._VALIDATION_ENVIRONMENT_KEYS:  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv(key, "STALE_VALIDATION_SENTINEL")

    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.PR_TRIAGE,
        _request(evidence_root=str(root), evidence_pr=837),
        attested_head=_EVIDENCE_HEAD,
    )
    resolved_root = os.path.realpath(root)
    assert launch_environment.bound_root is not None
    assert launch_environment.bound_root.kind is BoundRootKind.EVIDENCE
    assert launch_environment.bound_root.path == resolved_root

    environment = launch_environment.environment
    assert environment[EVIDENCE_ROOT_ENVIRONMENT_KEY] == resolved_root
    assert PLAN_ROOT_ENVIRONMENT_KEY not in environment
    assert not (
        set(environment) & usage_cli._VALIDATION_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    )

    # A role with no admitting policy sees the key stripped too, including the
    # stale value this test just set above.
    other_role_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.ENGINEER_BE, _request(), attested_head=_EVIDENCE_HEAD
    )
    assert other_role_environment.bound_root is None
    assert EVIDENCE_ROOT_ENVIRONMENT_KEY not in other_role_environment.environment
    assert PLAN_ROOT_ENVIRONMENT_KEY not in other_role_environment.environment


def test_evidence_bundle_rejects_missing_pr() -> None:
    """The evidence-pr grammar rejects an absent companion at the deep-validation layer too."""
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            "/evidence/root", None, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    "mutate",
    [
        "extra-file",
        "missing-file",
        "subdirectory",
        "symlinked-payload",
        "hardlinked-payload",
        "wrong-file-mode",
        "wrong-dir-mode",
        "foreign-uid-payload",
    ],
)
def test_evidence_bundle_structural_rejections(
    mutate: str, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12: every structural bundle break fails closed before manifest parsing."""
    base = tmp_path_factory.mktemp(f"evidence-structural-{mutate}")
    root = _build_evidence_bundle(base / "root")
    if mutate == "extra-file":
        (root / "extra.json").write_bytes(b"{}")
        (root / "extra.json").chmod(0o400)
    elif mutate == "missing-file":
        (root / "authors.json").unlink()
    elif mutate == "subdirectory":
        (root / "authors.json").unlink()
        (root / "authors.json").mkdir()
    elif mutate == "symlinked-payload":
        real = root / "authors.json"
        content = real.read_bytes()
        real.unlink()
        target = base / "real-authors.json"
        target.write_bytes(content)
        target.chmod(0o400)
        (root / "authors.json").symlink_to(target)
    elif mutate == "hardlinked-payload":
        target = base / "hardlink-target.json"
        os.link(root / "authors.json", target)
    elif mutate == "wrong-file-mode":
        os.chmod(root / "authors.json", 0o600)
    elif mutate == "wrong-dir-mode":
        os.chmod(root, 0o755)
    elif mutate == "foreign-uid-payload":
        real_fstat = os.fstat

        def foreign_fstat(descriptor: int) -> os.stat_result:
            status = real_fstat(descriptor)
            fields = list(status)
            fields[4] = status.st_uid + 1
            return os.stat_result(fields)

        monkeypatch.setattr(os, "fstat", foreign_fstat)

    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    "manifest_overrides",
    [
        {"evidence_schema_version": 2},
        {"repository": "syamaner/roastpilot-plan"},
        {"pull_request": 999},
        {"head_sha": "c" * 40},
        {"head_sha": "not-hex"},
        {"base_sha": "not-hex"},
        {"generated_at": "not-a-timestamp"},
    ],
    ids=[
        "wrong-version",
        "wrong-repository",
        "pr-mismatch",
        "head-sha-mismatch",
        "head-sha-non-hex",
        "base-sha-non-hex",
        "malformed-generated-at",
    ],
)
def test_evidence_bundle_manifest_field_rejections(
    manifest_overrides: dict[str, object], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T13: each malformed or mismatched manifest field fails closed."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-manifest") / "root", manifest_overrides=manifest_overrides
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_bundle_rejects_future_generated_at(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T13: a manifest timestamp beyond the 120-second skew fails closed."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-future") / "root", generated_at=future
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    "generated_at",
    [
        "2025-01-01T00:00:00",
        "2025-01-01T00:00:00.000000",
    ],
    ids=["naive-seconds", "naive-microseconds"],
)
def test_evidence_bundle_rejects_naive_generated_at(
    generated_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A timezone-naive ``generated_at`` fails closed rather than being treated as UTC."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-naive-generated-at") / "root",
        generated_at=generated_at,
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    "generated_at",
    [
        "2025-01-01T00:00:00+02:00",
        "2025-01-01T00:00:00-05:00",
        "2025-01-01T00:00:00+00:30",
    ],
    ids=["positive-offset", "negative-offset", "non-hour-offset"],
)
def test_evidence_bundle_rejects_non_zero_offset_generated_at(
    generated_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A non-zero UTC offset fails closed instead of being silently normalized."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-offset-generated-at") / "root",
        generated_at=generated_at,
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    "generated_at",
    [
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:00:00+00:00",
        "2025-01-01T00:00:00.123456+00:00",
    ],
    ids=["zulu", "explicit-zero-offset", "zero-offset-microseconds"],
)
def test_evidence_bundle_accepts_rfc3339_utc_generated_at(
    generated_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``Z`` and explicit ``+00:00`` RFC3339 UTC forms remain valid."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-utc-generated-at") / "root",
        generated_at=generated_at,
    )
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.kind is BoundRootKind.EVIDENCE


@pytest.mark.parametrize(
    "generated_at",
    [
        "2025-01-01 00:00:00+00:00",
        "20250101T000000Z",
        "2025-01-01T00:00:00-00:00",
    ],
    ids=["space-separator", "compact-basic", "unknown-local-offset"],
)
def test_evidence_bundle_rejects_non_rfc3339_shapes_generated_at(
    generated_at: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A syntactically-ISO-8601 but non-RFC3339-UTC shape fails closed.

    ``datetime.fromisoformat`` alone accepts a space separator, a
    compact/basic (no ``-``/``:``) date-time, and the RFC3339
    unknown-local-offset ``-00:00`` token; the closed positive grammar guard
    must reject all three before semantic parsing is ever reached.
    """
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-non-rfc3339-generated-at") / "root",
        generated_at=generated_at,
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_bundle_rejects_unknown_and_duplicate_manifest_keys(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T13: an unknown or duplicated top-level manifest key fails closed."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-unknown-key") / "root")
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(b'{"evidence_schema_version": 1, "unexpected_field": true}')
    manifest_path.chmod(0o400)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )

    duplicate_root = tmp_path_factory.mktemp("evidence-dup-key") / "root"
    _mkdir_exact(duplicate_root, 0o700)
    for name in EVIDENCE_PAYLOAD_FILES:
        (duplicate_root / name).write_bytes(b"{}")
        (duplicate_root / name).chmod(0o400)
    dup_manifest = duplicate_root / EVIDENCE_MANIFEST_NAME
    dup_manifest.write_bytes(b'{"evidence_schema_version": 1, "evidence_schema_version": 1}')
    dup_manifest.chmod(0o400)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(duplicate_root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_bundle_rejects_wrong_files_keyset(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T13: a ``files`` map missing or adding a payload key fails closed."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-files-keyset") / "root")
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    del manifest["files"]["authors.json"]
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(json.dumps(manifest).encode())
    manifest_path.chmod(0o400)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def _overwrite_manifest(root: Path, manifest: dict[str, object]) -> None:
    """Overwrite ``root``'s manifest with an arbitrary dict, preserving the 0400 mode."""
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(json.dumps(manifest).encode())
    manifest_path.chmod(0o400)


def _valid_manifest(root: Path) -> dict[str, object]:
    """Return the currently-written manifest dict for ``root``, for field-level mutation."""
    return json.loads((root / EVIDENCE_MANIFEST_NAME).read_bytes())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generated_at", 12345),
        ("head_sha", "not-hex"),
    ],
)
def test_evidence_bundle_rejects_non_string_or_non_hex_top_level_fields(
    field: str, value: object, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T13: a non-string ``generated_at`` and a non-hex ``head_sha`` both fail closed."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp(f"evidence-{field}") / "root")
    manifest = _valid_manifest(root)
    manifest[field] = value
    _overwrite_manifest(root, manifest)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize(
    ("mutate_entry", "entry"),
    [
        ("not-a-dict", "not-a-dict"),
        ("extra-key", {"sha256": "a" * 64, "bytes": 1, "extra": True}),
        ("non-hex-digest", {"sha256": "not-hex", "bytes": 1}),
        ("non-int-bytes", {"sha256": "a" * 64, "bytes": "1"}),
        ("negative-bytes", {"sha256": "a" * 64, "bytes": -1}),
        ("bool-bytes", {"sha256": "a" * 64, "bytes": True}),
    ],
)
def test_evidence_bundle_rejects_malformed_per_file_manifest_entries(
    mutate_entry: str, entry: object, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T13: every malformed per-file manifest entry shape fails closed."""
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp(f"evidence-entry-{mutate_entry}") / "root"
    )
    manifest = _valid_manifest(root)
    files = manifest["files"]
    assert isinstance(files, dict)
    files["authors.json"] = entry
    _overwrite_manifest(root, manifest)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_bundle_rejects_manifest_json_syntax_error(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T13: a manifest with invalid JSON syntax (not just a duplicate key) fails closed."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-json-syntax") / "root")
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(b"{not valid json at all")
    manifest_path.chmod(0o400)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_bundle_rejects_oversize_manifest(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T13/structural: a manifest exceeding the 64 KiB cap fails closed before parsing."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-oversize-manifest") / "root")
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(b" " + b"x" * (usage_models.EVIDENCE_MAX_MANIFEST_BYTES + 1))
    manifest_path.chmod(0o400)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


def test_evidence_listing_rejects_listdir_failure(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``os.listdir`` failure on the bundle root fails closed via the fixed error."""
    root_fd = os.open(
        str(tmp_path_factory.mktemp("evidence-listdir-fd") / "unused"), os.O_CREAT | os.O_RDONLY
    )
    os.close(root_fd)

    def failing_listdir(_descriptor: object) -> list[str]:
        raise OSError("SENTINEL_LISTDIR_FAILURE")

    monkeypatch.setattr(usage_cli.os, "listdir", failing_listdir)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid") as error:
        usage_cli._evidence_listing(0)  # pyright: ignore[reportPrivateUsage]
    assert "SENTINEL_LISTDIR_FAILURE" not in str(error.value)


def test_read_bounded_evidence_file_rejects_read_failure(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure on an already-opened bundle entry fails closed via the fixed error."""
    target = tmp_path_factory.mktemp("evidence-read-failure") / "file.json"
    target.write_bytes(b"{}")
    descriptor = os.open(str(target), os.O_RDONLY)

    class FailingStream:
        def read(self, _size: int) -> bytes:
            raise OSError("SENTINEL_READ_FAILURE")

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *_args: object) -> None:
            os.close(descriptor)

    def fake_fdopen(_fd: int, _mode: str) -> FailingStream:
        return FailingStream()

    monkeypatch.setattr(usage_cli.os, "fdopen", fake_fdopen)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid") as error:
        usage_cli._read_bounded_evidence_file(descriptor, 100)  # pyright: ignore[reportPrivateUsage]
    assert "SENTINEL_READ_FAILURE" not in str(error.value)


def test_hash_evidence_payload_rejects_read_failure(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure while streaming a payload fails closed via the fixed error."""
    target = tmp_path_factory.mktemp("evidence-hash-failure") / "file.json"
    target.write_bytes(b"{}")
    descriptor = os.open(str(target), os.O_RDONLY)

    class FailingStream:
        def read(self, _size: int) -> bytes:
            raise OSError("SENTINEL_HASH_READ_FAILURE")

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *_args: object) -> None:
            os.close(descriptor)

    def fake_fdopen(_fd: int, _mode: str) -> FailingStream:
        return FailingStream()

    monkeypatch.setattr(usage_cli.os, "fdopen", fake_fdopen)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid") as error:
        usage_cli._hash_evidence_payload(  # pyright: ignore[reportPrivateUsage]
            descriptor, remaining_budget=1000
        )
    assert "SENTINEL_HASH_READ_FAILURE" not in str(error.value)


def test_evidence_bundle_state_rejects_root_open_failure() -> None:
    """A root that cannot be opened fails closed via the fixed error, called directly."""
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._evidence_bundle_state(  # pyright: ignore[reportPrivateUsage]
            "/does/not/exist/at/all", 837, _EVIDENCE_HEAD
        )


def test_evidence_bundle_state_rejects_foreign_uid_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_evidence_bundle_state``'s own root ownership re-check fails closed on a foreign uid.

    Calls the function directly (bypassing the earlier shape validation, which
    would trip on the same monkeypatched ``fstat`` first) to isolate this
    redundant, defense-in-depth check.
    """
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-foreign-uid-state") / "root")
    real_fstat = os.fstat

    def foreign_fstat(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        fields = list(status)
        fields[4] = status.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_fstat)
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._evidence_bundle_state(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, _EVIDENCE_HEAD
        )


def test_evidence_bundle_reattest_catches_root_replaced_with_identical_content(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T15: a root replaced at the same path with byte-identical content still fails closed.

    Every structural, manifest, and integrity check passes against the
    identical rebuilt content, so only the outer snapshot's ``(st_dev,
    st_ino)`` inequality catches this — the one path that reaches the
    reattest closure's own comparison rather than raising inside
    ``_evidence_bundle_state`` itself.
    """
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-root-replaced") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    shutil.rmtree(root)
    _build_evidence_bundle(root)
    assert bound.reattest is not None
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        bound.reattest()


@pytest.mark.parametrize(
    "mutate", ["flipped-byte", "wrong-declared-size", "over-file-cap", "over-aggregate-cap"]
)
def test_evidence_bundle_integrity_rejections(
    mutate: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T14: every integrity break — digest, size, per-file cap, aggregate cap — fails closed."""
    base = tmp_path_factory.mktemp(f"evidence-integrity-{mutate}")
    root = _build_evidence_bundle(base / "root")
    if mutate == "flipped-byte":
        target = root / "authors.json"
        os.chmod(target, 0o600)
        target.write_bytes(b'{"authors": false}')
        os.chmod(target, 0o400)
        with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
            usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
                str(root), 837, attested_head=_EVIDENCE_HEAD
            )
        return
    if mutate == "wrong-declared-size":
        manifest_path = root / EVIDENCE_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"]["authors.json"]["bytes"] += 1
        os.chmod(manifest_path, 0o600)
        manifest_path.write_bytes(json.dumps(manifest).encode())
        os.chmod(manifest_path, 0o400)
        with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
            usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
                str(root), 837, attested_head=_EVIDENCE_HEAD
            )
        return
    if mutate == "over-file-cap":
        oversize = b"x" * (EVIDENCE_MAX_FILE_BYTES + 1)
        root = _build_evidence_bundle(
            tmp_path_factory.mktemp("evidence-over-file") / "root",
            payload_overrides={"authors.json": oversize},
        )
        with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
            usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
                str(root), 837, attested_head=_EVIDENCE_HEAD
            )
        return
    # over-aggregate-cap: eight files each just under the per-file cap.
    per_file = (EVIDENCE_MAX_TOTAL_BYTES // 8) + 1024
    overrides = {name: (b"y" * per_file) for name in EVIDENCE_PAYLOAD_FILES}
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-over-aggregate") / "root", payload_overrides=overrides
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )


@pytest.mark.parametrize("mutate", ["payload-modified", "payload-deleted", "extra-file-added"])
def test_evidence_bundle_post_exit_tamper_rejects(
    mutate: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T15: any post-exit bundle tamper fails closed with no record or handback."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp(f"evidence-tamper-{mutate}") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    if mutate == "payload-modified":
        os.chmod(root / "authors.json", 0o600)
        (root / "authors.json").write_bytes(b'{"authors": false}')
        os.chmod(root / "authors.json", 0o400)
    elif mutate == "payload-deleted":
        os.chmod(root, 0o700)
        (root / "authors.json").unlink()
    else:
        (root / "extra.json").write_bytes(b"{}")
        (root / "extra.json").chmod(0o400)
    assert bound.reattest is not None
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        bound.reattest()


def test_evidence_bundle_reattest_passes_when_untouched(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T15 (negative control): an untouched bundle's reattest succeeds."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-clean-reattest") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.reattest is not None
    bound.reattest()


def test_evidence_bundle_non_identity_payloads_remain_opaque(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T16: only ``pr.json`` is semantic; the other seven payloads remain opaque bytes."""
    overrides = {
        name: b"not json at all, just bytes" for name in EVIDENCE_PAYLOAD_FILES if name != "pr.json"
    }
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-non-json") / "root", payload_overrides=overrides
    )
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.kind is BoundRootKind.EVIDENCE


def test_evidence_bundle_never_leaks_into_errors_or_output(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T17: no path, digest, PR number, or payload byte reaches an exception or its chain."""
    sentinel_root = tmp_path_factory.mktemp("SENTINELEVIDENCEROOT")
    payload = b"SENTINEL_PAYLOAD_BYTE_CONTENT"
    root = _build_evidence_bundle(
        sentinel_root / "root", payload_overrides={"authors.json": payload}
    )
    os.chmod(root / "authors.json", 0o600)
    (root / "authors.json").write_bytes(payload + b"TAMPERED")
    os.chmod(root / "authors.json", 0o400)
    with pytest.raises(CaptureUsageError) as error:
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )
    message = str(error.value)
    assert "SENTINELEVIDENCEROOT" not in message
    assert "SENTINEL_PAYLOAD_BYTE_CONTENT" not in message
    assert "837" not in message
    assert _EVIDENCE_HEAD not in message
    cause = error.value.__cause__
    assert cause is None or "SENTINELEVIDENCEROOT" not in str(cause)


# --- End-to-end pipeline wiring (story-planner / pr-triage / product-auditor) ---


def test_native_launch_binds_plan_root_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A ``story-planner`` capture run binds the plan root, reattests, and appends a record."""
    plan_root = _build_plan_root(tmp_path_factory.mktemp("plan-e2e") / "root")
    plan_sha = "1" * 40
    resolved_plan_root = os.path.realpath(plan_root)
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved_plan_root, plan_sha))
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project, observed, processes, transcript=_read_only_transcript_bytes("story-planner")
        ),
    )
    assert (
        main(_native_cli_args(role="story-planner", plan_root=str(plan_root), plan_sha=plan_sha))
        == 0
    )
    worker_argv = observed[1][0]
    assert worker_argv[worker_argv.index("--add-dir") + 1] == resolved_plan_root
    assert "--allowedTools" not in worker_argv
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.STORY_PLANNER


def test_native_launch_binds_evidence_bundle_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A ``pr-triage`` capture run binds the evidence bundle, reattests, and appends a record."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)

    def fixed_attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        del post_exit
        return _EVIDENCE_HEAD

    monkeypatch.setattr(usage_cli, "_validate_native_worktree", fixed_attestation)
    evidence_root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-e2e") / "root")
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes_rebranded("security-reviewer", "pr-triage"),
        ),
    )
    assert (
        main(
            _native_cli_args(
                role="pr-triage",
                evidence_root=str(evidence_root),
                evidence_pr=837,
                base_sha=_EVIDENCE_HEAD,
            )
        )
        == 0
    )
    worker_argv = observed[1][0]
    assert worker_argv[worker_argv.index("--add-dir") + 1] == os.path.realpath(evidence_root)
    assert "--allowedTools" not in worker_argv
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.PR_TRIAGE


def test_native_launch_product_auditor_optional_plan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T8 (roster): ``product-auditor`` captures cleanly with and without the plan pair."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes_rebranded(
                "security-reviewer", "product-auditor"
            ),
        ),
    )
    assert main(_native_cli_args(role="product-auditor")) == 0
    unbound_argv = observed[1][0]
    assert "--add-dir" not in unbound_argv
    assert "--allowedTools" not in unbound_argv


def test_native_launch_product_auditor_with_plan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T8 (roster): a bound ``product-auditor`` plan pair produces exactly one ``--add-dir``."""
    plan_root = _build_plan_root(tmp_path_factory.mktemp("auditor-plan") / "root")
    plan_sha = "1" * 40
    resolved_plan_root = os.path.realpath(plan_root)
    monkeypatch.setattr(usage_cli, "_git_output", _fixed_plan_git(resolved_plan_root, plan_sha))
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_read_only_transcript_bytes_rebranded(
                "security-reviewer", "product-auditor"
            ),
        ),
    )
    assert (
        main(_native_cli_args(role="product-auditor", plan_root=str(plan_root), plan_sha=plan_sha))
        == 0
    )
    bound_argv = observed[1][0]
    assert bound_argv[bound_argv.index("--add-dir") + 1] == resolved_plan_root
    assert "--allowedTools" not in bound_argv


def test_negative_proof_plan_root_rejected_for_qa(tmp_path_factory: pytest.TempPathFactory) -> None:
    """L10(a): ``--plan-root`` supplied to a role that does not admit it rejects pre-launch."""
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._resolve_bound_root(  # pyright: ignore[reportPrivateUsage]
            NativeClaudeRole.QA,
            _request(plan_root="/plan/root", plan_sha="1" * 40),
            attested_head="0" * 40,
        )


def test_negative_proof_evidence_root_rejected_for_story_planner() -> None:
    """L10(b): ``--evidence-root`` supplied to a role that does not admit it rejects."""
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._resolve_bound_root(  # pyright: ignore[reportPrivateUsage]
            NativeClaudeRole.STORY_PLANNER,
            _request(evidence_root="/evidence/root", evidence_pr=1),
            attested_head="0" * 40,
        )


def test_regression_unbound_read_only_role_has_no_add_dir_or_allowed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T22/L11: an unbound READ_ONLY role's argv carries no ``--add-dir``/``--allowedTools``."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project, observed, processes, transcript=_read_only_transcript_bytes("safety-reviewer")
        ),
    )
    assert main(_native_cli_args(role="safety-reviewer")) == 0
    worker_argv = observed[1][0]
    assert "--add-dir" not in worker_argv
    assert "--allowedTools" not in worker_argv


# --- print-validation-commands: ALLOW / RUN grammar (D169, §2.5) -----------


def test_print_validation_commands_allow_then_run_blocks(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T18: output is exactly the ``ALLOW`` block then the ``RUN`` block; every RUN is covered."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    for role in usage_cli.VALIDATION_ENVIRONMENT_ROLES:
        exit_code = main(
            ["print-validation-commands", "--role", role.value, "--validation-root", str(root)]
        )
        assert exit_code == 0
        printed = capsys.readouterr().out.splitlines()
        rendered = render_validation_commands(role, resolved_root)
        commands = VALIDATION_ROLE_COMMANDS[role]
        allow_lines = [
            f"ALLOW {command.kind.value} {text}"
            for command, text in zip(commands, rendered, strict=True)
        ]
        run_lines = [f"RUN {text}" for text in rendered]
        assert printed == [*allow_lines, *run_lines]
        assert all(line.startswith("ALLOW ") for line in printed[: len(allow_lines)])
        assert all(line.startswith("RUN ") for line in printed[len(allow_lines) :])


def test_print_validation_commands_pytest_arg_renders_quoted_covered_run_line(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T19: tokens with space, quote, ``$``, ``;``, and a glob each round-trip through shlex."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    tokens = ["tests/has space.py", "it's", "$HOME", ";rm -rf", "tests/*.py"]
    args = ["print-validation-commands", "--role", "qa", "--validation-root", str(root)]
    for token in tokens:
        args.extend(["--pytest-arg", token])
    assert main(args) == 0
    printed = capsys.readouterr().out.splitlines()
    prefix = render_validation_commands(NativeClaudeRole.QA, resolved_root)[0]
    run_line = next(line for line in printed if line.startswith("RUN " + prefix))
    run_command = run_line.removeprefix("RUN ")
    assert shlex.split(run_command) == [*shlex.split(prefix), *tokens]
    assert run_command.startswith(prefix + " ")


def test_print_validation_commands_omitting_tokens_still_emits_bare_covered_run_line(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T19: omitting ``--pytest-arg`` still emits exactly one bare, covered ``RUN`` line."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    assert main(["print-validation-commands", "--role", "qa", "--validation-root", str(root)]) == 0
    printed = capsys.readouterr().out.splitlines()
    prefix = render_validation_commands(NativeClaudeRole.QA, resolved_root)[0]
    assert f"RUN {prefix}" in printed


@pytest.mark.parametrize("role", ["mcp-contract-checker", "sim-roast-runner"])
def test_print_validation_commands_rejects_pytest_arg_for_non_prefix_role(
    role: str, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T20: ``--pytest-arg`` for a role with no ``PREFIX`` entry rejects with no output."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                role,
                "--validation-root",
                str(root),
                "--pytest-arg",
                "tests/",
            ]
        )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("forbidden", ["has\nnewline", "has\rcarriage", "has\x00nul"])
def test_print_validation_commands_rejects_forbidden_pytest_arg_bytes(
    forbidden: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """T20: a token containing a forbidden byte rejects with no output, before any handler runs."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                "/validated/root",
                "--pytest-arg",
                forbidden,
            ]
        )
    assert capsys.readouterr().out == ""


def test_print_validation_commands_rejects_33_tokens(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T20: 33 tokens (one over the 32-token cap) rejects with no output."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    args = ["print-validation-commands", "--role", "qa", "--validation-root", str(root)]
    for index in range(33):
        args.extend(["--pytest-arg", f"t{index}"])
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(args)
    assert capsys.readouterr().out == ""


def test_print_validation_commands_rejects_257_byte_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T20: a 257-byte token rejects with no output, before any handler runs."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                "/validated/root",
                "--pytest-arg",
                "x" * 257,
            ]
        )
    assert capsys.readouterr().out == ""


def test_evidence_pr_option_rejects_non_numeric_value(capsys: pytest.CaptureFixture[str]) -> None:
    """The ``--evidence-pr`` type function rejects a non-numeric value before any handler runs."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [*_native_cli_args(role="pr-triage"), "--evidence-pr", "not-a-number"]
        )
    assert "positive integer" in capsys.readouterr().err.lower()


def test_evidence_pr_option_rejects_zero_and_negative_values() -> None:
    """The ``--evidence-pr`` type function rejects zero and negative integers."""
    for value in ("0", "-1"):
        with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
            usage_cli._evidence_pr(value)  # pyright: ignore[reportPrivateUsage]


def test_render_prefix_run_command_rejects_broken_coverage_proof() -> None:
    """M22: a malformed prefix that ``shlex.split`` cannot round-trip fails closed."""
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._render_prefix_run_command(  # pyright: ignore[reportPrivateUsage]
            "python -m pytest 'unterminated", ("tests/",)
        )


def test_print_validation_commands_rejects_broken_coverage_proof(
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T20/M22: defeated shell-quoting that breaks the round-trip rejects with no output."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")

    def identity_quote(value: str) -> str:
        return value

    monkeypatch.setattr(usage_cli.shlex, "quote", identity_quote)
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                str(root),
                "--pytest-arg",
                "has space",
            ]
        )
    assert capsys.readouterr().out == ""


def test_print_validation_commands_validates_root_exactly_once(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T21: one invocation performs exactly one root validation."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    real_validate = usage_cli._validate_validation_root  # pyright: ignore[reportPrivateUsage]
    calls: list[str] = []

    def counting_validate(raw: str) -> str:
        calls.append(raw)
        return real_validate(raw)

    monkeypatch.setattr(usage_cli, "_validate_validation_root", counting_validate)
    assert main(["print-validation-commands", "--role", "qa", "--validation-root", str(root)]) == 0
    assert calls == [str(root)]


# --- Role/doc content guards and class sweeps (T34-T37, §4) ----------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENT_FILES = tuple(sorted((_REPO_ROOT / ".claude" / "agents").glob("*.md")))
_CREDENTIALED_TOOL_PATTERN = re.compile(r"gh (pr|api|issue|run|search)|curl |wget ")


def test_no_agent_file_instructs_gh_curl_or_wget_invocation() -> None:
    """T34/class-sweep#1: no role file instructs `gh`/`curl`/`wget` (the story-planner
    prohibition of `gh issue view --comments` is an explicit non-instruction and is
    asserted present instead)."""
    matches: list[tuple[str, str]] = []
    for path in _AGENT_FILES:
        text = path.read_text()
        for line in text.splitlines():
            if _CREDENTIALED_TOOL_PATTERN.search(line) and "gh issue view --comments" not in line:
                matches.append((path.name, line.strip()))
    assert matches == []
    story_planner = (_REPO_ROOT / ".claude" / "agents" / "story-planner.md").read_text()
    assert "gh issue view --comments" in story_planner


def test_pr_triage_names_bundle_files_and_no_gh_rule() -> None:
    """T35: ``pr-triage.md`` names the nine files, the no-`gh` rule, and BLOCK-on-missing-datum."""
    text = (_REPO_ROOT / ".claude" / "agents" / "pr-triage.md").read_text()
    for name in EVIDENCE_BUNDLE_FILES:
        assert name in text
    assert "no `gh`" in text or "no `gh`, no network" in text
    assert "untrusted data" in text
    assert "BLOCK" in text
    assert "gh " not in text.replace("gh issue view --comments", "")


def test_plan_roles_have_no_default_checkout_command_position() -> None:
    """T36: planning-architect/story-planner/product-auditor name no default checkout."""
    for stem in ("planning-architect", "story-planner", "product-auditor"):
        text = (_REPO_ROOT / ".claude" / "agents" / f"{stem}.md").read_text()
        for match in re.finditer("roastpilot-plan", text):
            window = text[max(0, match.start() - 120) : match.end() + 120]
            # Only an explicit negation ("is **not** ...") or a description of the
            # parent-bound worktree may surround the historical default path.
            assert "not" in window or "parent" in window, window
        lowered = text.lower()
        assert "plan root" in lowered or "roastpilot-plan` worktree" in lowered


def test_validation_role_files_instruct_run_line_execution() -> None:
    """T37: qa/mcp-contract-checker/sim-roast-runner instruct executing only `RUN ` lines."""
    for path in _VALIDATION_ROLE_FILES:
        text = path.read_text()
        assert "RUN " in text
        assert "ALLOW" in text
        assert "never" in text and "executable" in text


def test_bound_root_literal_sweep_matches_only_expected_sites() -> None:
    """Class-sweep#3: every bound-root/add-dir literal is a policy, argv, doc, or test site."""
    pattern = re.compile(
        r"--add-dir|--validation-root|--plan-root|--evidence-root|"
        r"ROASTPILOT_(VALIDATION|PLAN|EVIDENCE)"
    )
    allowed_roots = (
        _REPO_ROOT / ".agents" / "skills",
        _REPO_ROOT / ".claude" / "agents",
        _REPO_ROOT / "docs",
        _REPO_ROOT / "tests",
        _REPO_ROOT / "AGENTS.md",
    )
    searched = (
        *(_REPO_ROOT / ".agents" / "skills").rglob("*.py"),
        *(_REPO_ROOT / ".agents" / "skills").rglob("*.md"),
        *(_REPO_ROOT / ".claude" / "agents").rglob("*.md"),
        *(_REPO_ROOT / "docs").rglob("*.md"),
        Path(__file__),
        _REPO_ROOT / "AGENTS.md",
    )
    for path in searched:
        if not pattern.search(path.read_text()):
            continue
        assert any(
            path == root or (root.is_dir() and root in path.parents) for root in allowed_roots
        ), path


def test_argv_construction_sweep_single_site() -> None:
    """Class-sweep#4: exactly one construction site builds ``--add-dir``/``--allowedTools``."""
    cli_text = (
        _REPO_ROOT
        / ".agents"
        / "skills"
        / "capture-agent-usage"
        / "scripts"
        / "capture_usage_cli.py"
    ).read_text()
    assert cli_text.count('"--add-dir"') == 1
    assert cli_text.count('"--allowedTools"') == 1
    generic_argv_index = cli_text.index("def _launch_argv(")
    native_argv_index = cli_text.index("def _native_claude_argv(")
    assert generic_argv_index < native_argv_index
    generic_body = cli_text[generic_argv_index:native_argv_index]
    assert "--add-dir" not in generic_body
    assert "--allowedTools" not in generic_body


def test_descriptor_command_rendering_sweep() -> None:
    """Class-sweep#5: ALLOW/RUN/EXACT/PREFIX literals are confined to rendering, docs, tests."""
    cli_text = (
        _REPO_ROOT
        / ".agents"
        / "skills"
        / "capture-agent-usage"
        / "scripts"
        / "capture_usage_cli.py"
    ).read_text()
    assert 'f"ALLOW {command.kind.value} {text}"' in cli_text
    assert 'f"RUN {text}"' in cli_text


def test_content_egress_sweep_adds_no_new_write_site() -> None:
    """Retained sweep: content-egress sites are the pre-existing set plus bounded reads only."""
    scripts_dir = _REPO_ROOT / ".agents" / "skills" / "capture-agent-usage" / "scripts"
    cli_text = (scripts_dir / "capture_usage_cli.py").read_text()
    # The new evidence-bundle reader must never write a payload byte anywhere.
    assert "sys.stdout.write" in cli_text
    write_call_count = cli_text.count("sys.stdout.write(")
    # print-validation-commands and the handback emitter are the only stdout writers.
    assert write_call_count == 2


def test_schema_version_literal_sweep_stays_at_three() -> None:
    """Retained sweep: no ``Literal[4]`` or bumped schema constant anywhere in the scripts."""
    scripts_dir = _REPO_ROOT / ".agents" / "skills" / "capture-agent-usage" / "scripts"
    for path in scripts_dir.glob("*.py"):
        text = path.read_text()
        assert "Literal[4]" not in text
    assert usage_models.NATIVE_WORKER_USAGE_SCHEMA_VERSION == 3


def test_gitignore_rule_and_no_production_import_sweep_unchanged() -> None:
    """Retained sweep: exactly one `.agent-usage/` gitignore rule; no production import.

    ``scripts/tooling_coverage.py`` is a pre-existing, untouched dev-tooling
    coverage-source path string (not a production runtime import) and is
    excluded from this sweep on that basis.
    """
    gitignore = (_REPO_ROOT / ".gitignore").read_text()
    matches = [line for line in gitignore.splitlines() if line.strip() == ".agent-usage/"]
    assert len(matches) == 1
    target = _REPO_ROOT / "src"
    assert target.exists()
    for path in target.rglob("*.py"):
        assert "agent-usage" not in path.read_text()


def test_no_fable_case_sensitive_literal_anywhere() -> None:
    """Retained sweep: the literal ``Fable``/``claude-fable-5`` never appears (case-sensitive)
    in the capture skill's own scripts (never case-insensitive: that drowns in
    "spoofable"/"diffable" elsewhere in the repo, per the retained-sweep note)."""
    scripts_dir = _REPO_ROOT / ".agents" / "skills" / "capture-agent-usage" / "scripts"
    for path in scripts_dir.glob("*.py"):
        text = path.read_text()
        assert "Fable" not in text
        assert "claude-fable-5" not in text
