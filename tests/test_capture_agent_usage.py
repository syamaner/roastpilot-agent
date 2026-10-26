"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import importlib.util
import inspect
import itertools
import json
import math
import os
import pwd
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
import venv
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, NoReturn, cast
from uuid import UUID

import capture_usage_claude as usage_claude
import capture_usage_cli as usage_cli
import capture_usage_codex as usage_codex
import capture_usage_models as usage_models
import capture_usage_native_codex as usage_native_codex
import capture_usage_transcript as usage_transcript
import pytest
import test_worktree_gate_recipe as worktree_gate_recipe
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
    NATIVE_CODEX_CONFIG_SHA256,
    NATIVE_CODEX_REPOSITORY,
    NATIVE_CODEX_ROLE_INSTRUCTION_SHA256,
    NATIVE_CODEX_ROLE_SHA256,
    NATIVE_ROLE_EXCLUSIONS,
    PLAN_ROOT_ENVIRONMENT_KEY,
    VALIDATION_ENVIRONMENT_ROLES,
    VALIDATION_ENVIRONMENT_VERIFY_COMMAND,
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
    NativeCodexRole,
    NativeCodexTaskStatus,
    NativeCodexUsageRecord,
    NativeWorkerUsageRecord,
    ParsedUsage,
    RoleCapability,
    TaskUsageRecord,
    UsageRecord,
    ValidationCommandKind,
    bounded_jsonl_lines,
    is_scrubbed_environment_name,
    render_allowed_tools,
    render_gate_environment,
    render_gate_scrub_prefix,
    render_validation_commands,
    validation_environment_values,
)
from capture_usage_models import (
    USAGE_RECORD_ADAPTER as _USAGE_RECORD_ADAPTER,  # pyright: ignore[reportUnknownVariableType]
)
from pydantic import TypeAdapter, ValidationError

_REAL_VALIDATE_WORKTREE_METADATA = usage_cli._validate_worktree_metadata  # pyright: ignore[reportPrivateUsage]
_REAL_HARNESS_VERSION = usage_cli._harness_version  # pyright: ignore[reportPrivateUsage]
_REAL_HARNESS_VERSION_PROBE = usage_cli._harness_version_probe  # pyright: ignore[reportPrivateUsage]


def _stub_native_codex_git_administration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass Git-admin I/O in supervisor units that deliberately use synthetic roots."""
    administration = usage_native_codex._GitAdministration(  # pyright: ignore[reportPrivateUsage]
        None,
        usage_native_codex._Root(-1, 0, 0),  # pyright: ignore[reportPrivateUsage]
        None,
        None,
    )

    def open_administration(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
    ) -> usage_native_codex._GitAdministration:  # pyright: ignore[reportPrivateUsage]
        return administration

    def assert_administration(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _administration: usage_native_codex._GitAdministration,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        return None

    def close_administration(
        _administration: usage_native_codex._GitAdministration,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        return None

    def assert_administration_trees(
        _administration: usage_native_codex._GitAdministration,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        return None

    monkeypatch.setattr(usage_native_codex, "_open_git_administration", open_administration)
    monkeypatch.setattr(usage_native_codex, "_assert_git_administration", assert_administration)
    monkeypatch.setattr(
        usage_native_codex, "_assert_git_administration_trees", assert_administration_trees
    )
    monkeypatch.setattr(usage_native_codex, "_close_git_administration", close_administration)


def _native_leaf_boundary(worktree: Path, effort: str) -> dict[str, object]:
    """Build the closed 0.147 managed authority shape for real native rollouts."""
    path = str(worktree)
    filesystem: dict[str, object] = {
        "kind": "restricted",
        "entries": [
            {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
            {"path": {"type": "path", "path": path}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "slash_tmp"}}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"},
            {
                "path": {"type": "path", "path": str(worktree / ".git")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
            {
                "path": {"type": "path", "path": str(worktree / ".agents")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
            {
                "path": {"type": "path", "path": str(worktree / ".codex")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
        ],
    }
    return {
        "workspace_roots": [path],
        "sandbox_policy": {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": False,
            "exclude_slash_tmp": False,
        },
        "file_system_sandbox_policy": filesystem,
        "permission_profile": {
            "type": "managed",
            "file_system": filesystem,
            "network": "restricted",
        },
        "approval_policy": "on-request",
        "approvals_reviewer": "auto_review",
        "realtime_active": False,
        "collaboration_mode": {
            "mode": "default",
            "settings": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": effort,
                "developer_instructions": None,
            },
        },
    }


FIXTURES = Path(__file__).parent / "fixtures" / "agent-usage"
_HISTORICAL_CODEX = FIXTURES / "native-codex-gpt-5.6-terra"
_LIVE_CODEX = Path(__file__).resolve().parents[1] / ".codex"
_LIVE_CODEX_MODEL = "gpt-6.1-sol"
USAGE_RECORD_ADAPTER = cast(TypeAdapter[UsageRecord], _USAGE_RECORD_ADAPTER)


def _copy_historical_codex(destination: Path) -> Path:
    """Copy the frozen admitted native-Codex role bytes into a temporary root."""
    copied = destination / ".codex"
    shutil.copytree(_HISTORICAL_CODEX, copied)
    return copied


def test_observed_claude_version_binds_generic_stream_exactly() -> None:
    """A supplied installed-version stream admits only its probed version."""
    content = (FIXTURES / "claude-2.1.241.jsonl").read_bytes()

    usage = usage_claude.parse_claude_stream(
        BytesIO(content), expected_version="2.1.241", require_launch_authority=True
    )

    assert usage.input_tokens == 2
    with pytest.raises(ClaudeUsageParseError, match="unverified Claude version"):
        usage_claude.parse_claude_stream(
            BytesIO(content), expected_version="2.1.240", require_launch_authority=True
        )


def test_parse_claude_command_self_derives_init_after_leading_valid_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline inspection derives from init after a valid leading non-init event."""
    content = (FIXTURES / "claude-2.1.241.jsonl").read_bytes()
    stream = tmp_path / "stream.jsonl"
    lines = content.splitlines()
    stream.write_bytes(b"\n".join((lines[1], lines[0], *lines[2:])) + b"\n")
    assert main(["parse-claude", str(stream)]) == 0
    assert '"input_tokens":2' in capsys.readouterr().out

    malformed_init = json.loads(lines[0])
    malformed_init["claude_code_version"] = 1
    malformed_stream = (
        b"\n".join((lines[1], json.dumps(malformed_init).encode(), *lines[2:])) + b"\n"
    )
    with pytest.raises(ClaudeUsageParseError, match="unverified Claude version"):
        parse_claude_stream(
            BytesIO(malformed_stream), expected_version=None, require_launch_authority=False
        )
    stream.write_bytes(malformed_stream)
    with pytest.raises(SystemExit, match="Claude usage stream is invalid"):
        main(["parse-claude", str(stream)])

    missing_init_stream = b"\n".join((lines[1], *lines[2:])) + b"\n"
    with pytest.raises(ClaudeAuthorityError, match="init authority is not attested"):
        parse_claude_stream(
            BytesIO(missing_init_stream), expected_version=None, require_launch_authority=False
        )
    stream.write_bytes(missing_init_stream)
    with pytest.raises(SystemExit, match="Claude launch authority is not attested"):
        main(["parse-claude", str(stream)])


def test_observed_transcript_version_and_atis_latch_are_exact_and_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated latch rows validate exactly and retain neither latch value nor version drift."""
    session_id = "71111111-1111-4111-8111-111111111111"
    content = (FIXTURES / "claude-2.1.241-transcript" / "engineer-be.jsonl").read_bytes()
    transcript, installed_session = _install_owned_transcript(
        tmp_path, monkeypatch, content, session_id
    )

    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        installed_session,
        NativeClaudeRole.ENGINEER_BE,
        "high",
        expected_version="2.1.241",
        expected_permission_mode="auto",
    )

    assert usage.input_tokens == 2
    assert "SYNTHETIC_ATIS_SENTINEL" not in repr(usage)
    for old, new in (
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A",', b'"unexpected":true,'),
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A",', b""),
        (b'"sessionId":"71111111-1111-4111-8111-111111111111"', b'"sessionId":1'),
    ):
        transcript.write_bytes(content.replace(old, new))
        with pytest.raises(
            usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
        ):
            usage_transcript.parse_owned_transcript(
                tmp_path,
                installed_session,
                NativeClaudeRole.ENGINEER_BE,
                "high",
                expected_version="2.1.241",
                expected_permission_mode="auto",
            )


def test_observed_read_only_transcript_latches_and_handback_are_exact_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T11a: READ_ONLY 2.1.241 latches validate repeatedly without retained disclosure."""
    session_id = "72222222-2222-4222-8222-222222222222"
    content = (FIXTURES / "claude-2.1.241-transcript" / "story-planner.jsonl").read_bytes()
    transcript, installed_session = _install_owned_transcript(
        tmp_path, monkeypatch, content, session_id
    )
    usage = usage_transcript.parse_owned_transcript(
        tmp_path,
        installed_session,
        NativeClaudeRole.STORY_PLANNER,
        "high",
        expected_version="2.1.241",
        expected_permission_mode="dontAsk",
        require_handback=True,
    )
    captured = capsys.readouterr()
    assert usage.handback_text == "SYNTHETIC_STORY_HANDOFF"
    assert "SYNTHETIC_ATIS_SENTINEL" not in repr(usage)
    assert "SYNTHETIC_ATIS_SENTINEL" not in (usage.handback_text or "")
    assert captured.out == captured.err == ""
    replacements = (
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A",', b'"unexpected":true,'),
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A",', b""),
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A"', b'"atis":1'),
        (b'"atis":"SYNTHETIC_ATIS_SENTINEL_A"', b'"atis":{}'),
        (b'"sessionId":"72222222-2222-4222-8222-222222222222",', b""),
        (b'"sessionId":"72222222-2222-4222-8222-222222222222"', b'"sessionId":1'),
        (b'"sessionId":"72222222-2222-4222-8222-222222222222"', b'"sessionId":"foreign"'),
    )
    for old, new in replacements:
        transcript.write_bytes(content.replace(old, new, 1))
        with pytest.raises(
            usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
        ):
            usage_transcript.parse_owned_transcript(
                tmp_path,
                installed_session,
                NativeClaudeRole.STORY_PLANNER,
                "high",
                expected_version="2.1.241",
                expected_permission_mode="dontAsk",
                require_handback=True,
            )
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""


def test_observed_native_2_1_241_opaque_fields_are_not_retained_and_schema_stays_closed() -> None:
    """T12a: observed additive fields parse, while terminal usage schemas reject extras."""
    content = (FIXTURES / "claude-2.1.241-native.jsonl").read_bytes()
    usage = usage_claude.parse_claude_stream(
        BytesIO(content), expected_version="2.1.241", require_launch_authority=True
    )
    assert usage.input_tokens == 2
    assert "memory_paths" not in repr(usage)
    assert "rate_limit_info" not in repr(usage)
    assert "context_management" not in repr(usage)
    rows = [json.loads(line) for line in content.splitlines()]
    for container in (rows[-1]["usage"], rows[-1]["modelUsage"]["synthetic-native"]):
        container["SENTINEL_EXTRA"] = 0
        with pytest.raises(ClaudeUsageParseError, match="schema") as error:
            usage_claude.parse_claude_stream(
                _stream("\n".join(json.dumps(row) for row in rows) + "\n"),
                expected_version="2.1.241",
                require_launch_authority=True,
            )
        assert "SENTINEL_EXTRA" not in str(error.value)
        del container["SENTINEL_EXTRA"]


def test_native_codex_record_requires_closed_success_and_git_outcomes() -> None:
    """Native Codex records never claim success without a descendant result."""
    started = datetime.now(UTC)
    payload: dict[str, object] = {
        "captured_at": started,
        "task_id": "task-811",
        "slice_id": "native-codex-capture",
        "parent_task_id": "parent-811",
        "task_name": "native-codex-capture",
        "native_role": NativeCodexRole.ENGINEER_BE,
        "effort": "high",
        "config_sha256": NATIVE_CODEX_CONFIG_SHA256,
        "role_sha256": NATIVE_CODEX_ROLE_SHA256[NativeCodexRole.ENGINEER_BE],
        "repository": NATIVE_CODEX_REPOSITORY,
        "branch": "feature/811-native-codex-capture",
        "base_sha": "a" * 40,
        "launch_head_sha": "a" * 40,
        "final_head_sha": "b" * 40,
        "parent_thread_id": "thread-811",
        "leaf_session_id": "leaf-811",
        "harness_version": "0.147.0",
        "exit_code": None,
        "task_status": NativeCodexTaskStatus.SUCCESS,
        "success": True,
        "started_at": started,
        "completed_at": started,
        "elapsed_ms": 0,
        "input_tokens": 5,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 3,
        "output_tokens": 6,
        "reasoning_output_tokens": 5,
        "total_tokens": 11,
        "whole_tree_verified": True,
        "subagent_count": 0,
    }
    record = NativeCodexUsageRecord.model_validate(payload)
    assert USAGE_RECORD_ADAPTER.validate_json(record.model_dump_json()) == record
    for field in ("captured_at", "started_at", "completed_at"):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, field: started.replace(tzinfo=None)})
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, field: "2026-08-21T12:00:00"})
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate(
            {**payload, "captured_at": started + timedelta(milliseconds=1)}
        )
    for field, value in (
        ("success", "true"),
        ("whole_tree_verified", 1),
        ("schema_version", "1"),
        ("topology_depth", True),
        ("elapsed_ms", 0.0),
        ("input_tokens", True),
        ("cached_input_tokens", "2"),
        ("cache_write_input_tokens", 3.0),
        ("output_tokens", "6"),
        ("reasoning_output_tokens", False),
        ("total_tokens", 11.0),
        ("subagent_count", "0"),
    ):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, field: value})
    invalid_harness_versions: tuple[object, ...] = (
        None,
        {},
        True,
        1,
        "0.147",
        "v0.147.0",
        "0.147.0.1",
    )
    for value in invalid_harness_versions:
        with pytest.raises(ValidationError, match="harness version"):
            NativeCodexUsageRecord.model_validate({**payload, "harness_version": value})
    assert (
        NativeCodexUsageRecord.model_validate(
            {**payload, "harness_version": "99.100.101"}
        ).harness_version
        == "99.100.101"
    )
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate({**payload, "final_head_sha": "a" * 40})
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate(
            {**payload, "success": False, "task_status": NativeCodexTaskStatus.SUCCESS}
        )
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate(
            {**payload, "whole_tree_verified": False, "subagent_count": 1}
        )
    for elapsed in (1,):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, "elapsed_ms": elapsed})
    for offset in (1, -1):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate(
                {
                    **payload,
                    "completed_at": started + timedelta(milliseconds=offset),
                    "elapsed_ms": 0,
                }
            )
    for field, value in (
        ("config_sha256", "0" * 64),
        ("role_sha256", "0" * 64),
        ("repository", "syamaner/roastpilot-plan"),
    ):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, field: value})
        with pytest.raises(ValidationError):
            USAGE_RECORD_ADAPTER.validate_python({**payload, field: value})
    for field, value in (
        ("cached_input_tokens", 6),
        ("reasoning_output_tokens", 7),
        ("total_tokens", 12),
    ):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, field: value})
    for field in ("base_sha", "launch_head_sha", "final_head_sha"):
        for value in ("a" * 39, "A" * 40, "g" * 40):
            with pytest.raises(ValidationError):
                NativeCodexUsageRecord.model_validate({**payload, field: value})
    for branch in ("-feature", "feature/../x", "feature:811", "feature\n811"):
        with pytest.raises(ValidationError):
            NativeCodexUsageRecord.model_validate({**payload, "branch": branch})
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate({**payload, "launch_head_sha": "b" * 40})
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate(
            {
                **payload,
                "completed_at": cast(datetime, payload["started_at"]) - timedelta(milliseconds=1),
            }
        )


def test_native_codex_historical_fixture_matches_bound_hashes() -> None:
    """Frozen historical native-Codex bytes still match the capture admission hashes."""
    assert hashlib.sha256((_HISTORICAL_CODEX / "config.toml").read_bytes()).hexdigest() == (
        NATIVE_CODEX_CONFIG_SHA256
    )
    for role in NativeCodexRole:
        role_path = _HISTORICAL_CODEX / "agents" / f"{role.value}.toml"
        assert hashlib.sha256(role_path.read_bytes()).hexdigest() == NATIVE_CODEX_ROLE_SHA256[role]
        instructions = tomllib.loads(role_path.read_text())["developer_instructions"]
        assert isinstance(instructions, str)
        assert (
            hashlib.sha256(instructions.encode("utf-8")).hexdigest()
            == NATIVE_CODEX_ROLE_INSTRUCTION_SHA256[role]
        )


def test_native_codex_historical_registration_closure_is_admitted(tmp_path: Path) -> None:
    """The frozen historical registration closure remains admitted by capture."""
    expected = {
        NativeCodexRole.ENGINEER_BE: "high",
        NativeCodexRole.ENGINEER_FE: "high",
        NativeCodexRole.REPAIR: "medium",
    }
    _copy_historical_codex(tmp_path)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        for role, effort in expected.items():
            _config_hash, _role_hash, observed_effort, canonical = (
                usage_native_codex._registered_role(  # pyright: ignore[reportPrivateUsage]
                    root, role
                )
            )
            assert observed_effort == effort
            assert canonical == role.value
    finally:
        os.close(root.descriptor)


def test_native_codex_live_registered_roles_are_unsupported() -> None:
    """Live D206 role definitions fail capture admission before they can be recorded."""
    root = usage_native_codex._open_root(str(_LIVE_CODEX.parent), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        for role in NativeCodexRole:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._registered_role(root, role)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_live_roles_change_only_model_and_effort_lines() -> None:
    """D206 changes exactly the two admitted pin lines while config remains frozen."""
    assert (_LIVE_CODEX / "config.toml").read_bytes() == (
        _HISTORICAL_CODEX / "config.toml"
    ).read_bytes()
    for role in NativeCodexRole:
        historical = (
            (_HISTORICAL_CODEX / "agents" / f"{role.value}.toml")
            .read_bytes()
            .splitlines(keepends=True)
        )
        live = (
            (_LIVE_CODEX / "agents" / f"{role.value}.toml").read_bytes().splitlines(keepends=True)
        )
        expected = historical[:]
        expected[0] = f'model = "{_LIVE_CODEX_MODEL}"\n'.encode()
        expected[1] = b'model_reasoning_effort = "medium"\n'
        assert live == expected


@pytest.mark.parametrize("value", [0, 1, 131071])
def test_native_codex_registration_requires_committed_project_doc_limit(
    tmp_path: Path, value: int
) -> None:
    """Any project-doc byte-limit drift blocks native role attestation before READY."""
    _copy_historical_codex(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.write_text(
        config.read_text().replace(
            "project_doc_max_bytes = 131072", f"project_doc_max_bytes = {value}"
        )
    )
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._registered_role(root, NativeCodexRole.ENGINEER_BE)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "top_disabled",
        "model",
        "effort",
        "model_isolated",
        "effort_isolated",
        "hash_only",
        "leaf_enabled",
        "extra",
        "wrong_path",
        "no_model_boundary",
    ],
)
def test_native_codex_registered_role_rejects_tampered_authority(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every registered-role authority field is closed and fail-closed on drift."""
    config = (_HISTORICAL_CODEX / "config.toml").read_bytes()
    role = (_HISTORICAL_CODEX / "agents" / "engineer-be.toml").read_bytes()
    original_role = role
    if case == "top_disabled":
        config = config.replace(b"enabled = true", b"enabled = false", 1)
    elif case == "wrong_path":
        config = config.replace(b"agents/engineer-be.toml", b"agents/wrong.toml", 1)
    elif case == "model":
        role = role.replace(b"gpt-5.6-terra", b"gpt-5.6-other")
    elif case == "effort":
        role = role.replace(b'model_reasoning_effort = "high"', b'model_reasoning_effort = "low"')
    elif case == "model_isolated":
        lines = role.splitlines(keepends=True)
        lines[0] = f'model = "{_LIVE_CODEX_MODEL}"\n'.encode()
        role = b"".join(lines)
        monkeypatch.setitem(
            usage_models.NATIVE_CODEX_ROLE_SHA256,
            NativeCodexRole.ENGINEER_BE,
            hashlib.sha256(role).hexdigest(),
        )
    elif case == "effort_isolated":
        lines = role.splitlines(keepends=True)
        lines[1] = b'model_reasoning_effort = "medium"\n'
        role = b"".join(lines)
        monkeypatch.setitem(
            usage_models.NATIVE_CODEX_ROLE_SHA256,
            NativeCodexRole.ENGINEER_BE,
            hashlib.sha256(role).hexdigest(),
        )
    elif case == "hash_only":
        role += b"# comment\n"
    elif case == "leaf_enabled":
        role = role.replace(b"enabled = false", b"enabled = true")
    elif case == "extra":
        role += b"extra = true\n"
    elif case == "no_model_boundary":
        role = role.replace(b"invoke Claude Code or any other model", b"invoke provider")
    elif case != "none":  # pragma: no cover - parametrization is closed above.
        raise AssertionError(case)

    def read(_root: object, parts: tuple[str, ...]) -> bytes:
        return config if parts[-1] == "config.toml" else role

    monkeypatch.setattr(usage_native_codex, "_read_relative", read)
    root = usage_native_codex._Root(-1, 0, 0)  # pyright: ignore[reportPrivateUsage]
    if case == "none":
        assert role == original_role
        _config_hash, _role_hash, effort, canonical = usage_native_codex._registered_role(  # pyright: ignore[reportPrivateUsage]
            root, NativeCodexRole.ENGINEER_BE
        )
        assert effort == "high"
        assert canonical == NativeCodexRole.ENGINEER_BE.value
    else:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._registered_role(root, NativeCodexRole.ENGINEER_BE)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("depth", "ordinal_case", "raises", "expected_match"),
    [
        (1, "contiguous", False, True),
        (True, "contiguous", True, False),
        (1.0, "contiguous", True, False),
        (0, "contiguous", False, False),
        (2, "contiguous", False, False),
        (1, "first-nonzero", True, False),
        (1, "gap", True, False),
        (1, "duplicate", True, False),
        (1, "regression", True, False),
        (1, "removed-final-token", True, False),
    ],
)
def test_native_codex_rollout_uses_final_cumulative_total_once(
    tmp_path: Path, depth: object, ordinal_case: str, raises: bool, expected_match: bool
) -> None:
    """The native rollout parser keeps the final cumulative total instead of summing events."""
    manifest: dict[str, str] = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    meta = {
        "type": "session_meta",
        "payload": {
            "id": "leaf-811",
            "agent_nickname": "worker-811",
            "agent_path": "/root/native-codex-capture",
            "agent_role": "engineer-be",
            "base_instructions": "discarded",
            "originator": "codex-tui",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "discarded",
            "history_mode": "discarded",
            "model_provider": "openai",
            "multi_agent_version": "v2",
            "parent_thread_id": "parent-811",
            "session_id": "leaf-811",
            "thread_source": "subagent",
            "timestamp": "discarded",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent-811",
                        "depth": depth,
                        "agent_path": "/root/native-codex-capture",
                        "agent_nickname": "worker-811",
                        "agent_role": "engineer-be",
                    }
                }
            },
            "git": {
                "commit_hash": "a" * 40,
                "branch": "main",
                "repository_url": "discarded",
            },
        },
    }
    context: dict[str, object] = {
        "type": "turn_context",
        "payload": {
            "model": "gpt-5.6-terra",
            "effort": "high",
            "approval_policy": "discarded",
            "approvals_reviewer": "discarded",
            "collaboration_mode": "discarded",
            "comp_hash": "discarded",
            "current_date": "discarded",
            "cwd": "discarded",
            "file_system_sandbox_policy": "discarded",
            "multi_agent_version": "v2",
            "permission_profile": "discarded",
            "personality": "discarded",
            "realtime_active": False,
            "sandbox_policy": "discarded",
            "summary": "discarded",
            "timezone": "discarded",
            "turn_id": "discarded",
            "workspace_roots": [],
        },
    }

    def event(total: int) -> dict[str, object]:
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {},
                "info": {
                    "last_token_usage": {
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 0,
                    },
                    "model_context_window": 1,
                    "total_token_usage": {
                        "input_tokens": total,
                        "cached_input_tokens": min(total, 2),
                        "cache_write_input_tokens": 3,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 4,
                        "total_tokens": total + 4,
                    },
                },
            },
        }

    events = cast(
        list[dict[str, object]],
        [
            meta,
            context,
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "collaboration_mode_kind": "discarded",
                    "model_context_window": 1,
                    "started_at": "discarded",
                    "turn_id": "discarded",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "discarded",
                    "content": "SECRET_PROMPT_OR_RESPONSE",
                    "internal_chat_message_metadata_passthrough": {},
                    "role": "assistant",
                    "phase": "final",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "id": "discarded",
                    "call_id": "discarded",
                    "arguments": "SECRET_TOOL_INPUT",
                    "internal_chat_message_metadata_passthrough": {},
                    "name": "discarded",
                },
            },
            {"type": "world_state", "payload": {"full": False, "state": {}}},
            event(1),
            event(9),
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "completed_at": "discarded",
                    "duration_ms": 1,
                    "last_agent_message": "discarded",
                    "started_at": "discarded",
                    "time_to_first_token_ms": 1,
                    "turn_id": "discarded",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "completed_at_ms": 1,
                    "item": {},
                    "started_at_ms": 0,
                    "thread_id": "discarded",
                    "turn_id": "discarded",
                },
            },
        ],
    )
    ordinals = list(range(len(events)))
    if ordinal_case == "first-nonzero":
        ordinals[0] = 1
    elif ordinal_case == "gap":
        ordinals[4] = 5
    elif ordinal_case == "duplicate":
        ordinals[4] = 3
    elif ordinal_case == "regression":
        ordinals[5] = 3
    elif ordinal_case == "removed-final-token":
        events.pop(7)
        ordinals.pop(7)
    rollout = tmp_path / "leaf.jsonl"
    rollout.write_text(
        "".join(
            json.dumps({"ordinal": ordinal, "timestamp": "discarded", **item}) + "\n"
            for ordinal, item in zip(ordinals, events, strict=True)
        )
    )
    with rollout.open("rb") as stream:
        if raises:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._parse_rollout(stream.fileno(), manifest, "0.147.0")  # pyright: ignore[reportPrivateUsage]
            return
        session, totals, _parent, matches, consumed = usage_native_codex._parse_rollout(  # pyright: ignore[reportPrivateUsage]
            stream.fileno(), manifest, "0.147.0"
        )
    assert session == "leaf-811"
    assert matches is expected_match
    assert totals == (9, 2, 3, 4, 4, 13)
    assert consumed == rollout.stat().st_size


@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": 9,
            "cached_input_tokens": 1,
            "cache_write_input_tokens": 3,
            "output_tokens": 4,
            "reasoning_output_tokens": 5,
            "total_tokens": 13,
        },
        {
            "input_tokens": 9,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 3,
            "output_tokens": 4,
            "reasoning_output_tokens": 3,
            "total_tokens": 13,
        },
        {
            "input_tokens": 9,
            "cached_input_tokens": 2,
            "cache_write_input_tokens": 3,
            "output_tokens": 4,
            "reasoning_output_tokens": 5,
            "total_tokens": 14,
        },
    ],
)
def test_native_codex_rejects_contradictory_provider_token_totals(usage: dict[str, int]) -> None:
    """0.147 totals retain only the provider's non-overlapping token aggregate."""
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._totals(usage)  # pyright: ignore[reportPrivateUsage]


def test_native_codex_accepts_provider_token_total_boundaries() -> None:
    """Cached input and reasoning may equal their containing input/output categories."""
    usage = {
        "input_tokens": 9,
        "cached_input_tokens": 9,
        "cache_write_input_tokens": 3,
        "output_tokens": 4,
        "reasoning_output_tokens": 4,
        "total_tokens": 13,
    }
    assert usage_native_codex._totals(usage) == (9, 9, 3, 4, 4, 13)  # pyright: ignore[reportPrivateUsage]


def test_native_codex_inventory_does_not_read_or_size_existing_rollouts(tmp_path: Path) -> None:
    """A pre-existing large provider session is retained as an identity only."""
    existing = tmp_path / "existing.jsonl"
    existing.write_bytes(b"x" * (usage_native_codex.MAX_PROVIDER_FILE_BYTES + 1))
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        inventory = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)
    status = existing.stat()
    assert inventory == {(status.st_dev, status.st_ino, status.st_ctime_ns)}


def test_native_codex_inventory_closes_large_historical_rollouts_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full historical snapshot retains attestations, not one file descriptor per row."""
    historical_count = 128
    for index in range(historical_count):
        (tmp_path / f"history-{index}.jsonl").write_text("{}\n")
    monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_FILES", historical_count)
    monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_ENTRIES", historical_count)
    closed: list[int] = []
    real_close = usage_native_codex._close  # pyright: ignore[reportPrivateUsage]

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(usage_native_codex, "_close", close)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        inventory = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)

    assert len(inventory) == historical_count
    assert len(closed) >= historical_count
    assert all(not hasattr(entry, "descriptor") for entry in inventory.entries.values())


@pytest.mark.parametrize("mode", [0o620, 0o666])
def test_native_codex_provider_walk_rejects_writable_rollout_files(
    tmp_path: Path, mode: int
) -> None:
    """Provider rollout files may not grant group or world write access."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    os.chmod(rollout, mode)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_provider_walk_accepts_owner_read_write_rollout_file(tmp_path: Path) -> None:
    """The observed provider-owned 0644 rollout mode remains admitted."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    os.chmod(rollout, 0o644)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        assert len(usage_native_codex._inventory(root)) == 1  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize(
    ("target", "mode"), [("root", 0o777), ("root", 0o770), ("nested", 0o777), ("nested", 0o770)]
)
def test_native_codex_provider_walk_rejects_writable_directories(
    tmp_path: Path, target: str, mode: int
) -> None:
    """Provider sessions directories cannot grant group or world write access."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "rollout.jsonl").write_text("{}\n")
    os.chmod(tmp_path, 0o755)
    os.chmod(tmp_path if target == "root" else nested, mode)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_provider_walk_accepts_safe_root_and_nested_directories(
    tmp_path: Path,
) -> None:
    """Provider-owned 0755 sessions roots and nested directories remain admitted."""
    nested = tmp_path / "nested"
    nested.mkdir()
    rollout = nested / "rollout.jsonl"
    rollout.write_text("{}\n")
    for path in (tmp_path, nested):
        os.chmod(path, 0o755)
    os.chmod(rollout, 0o644)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        assert len(usage_native_codex._inventory(root)) == 1  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_parser_rejects_writable_rollout_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writable rollout descriptor fails before parser content handling."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    os.chmod(rollout, 0o620)

    def unexpected_json(_raw: bytes) -> object:
        pytest.fail("writable rollout reached content parsing")

    monkeypatch.setattr(usage_native_codex, "_json", unexpected_json)
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_inventory_rejects_renamed_pre_ready_inode(tmp_path: Path) -> None:
    """Renaming an old rollout after READY cannot turn its inode into new evidence."""
    existing = tmp_path / "existing.jsonl"
    existing.write_text("{}\n")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
        os.rename(existing, tmp_path / "renamed.jsonl")
        candidates = usage_native_codex._new_rollouts(root, before)  # pyright: ignore[reportPrivateUsage]
        assert candidates == []
    finally:
        os.close(root.descriptor)


def test_native_codex_new_rollouts_rejects_reused_pre_ready_inode_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed generation on a pre-READY inode cannot become fresh evidence."""
    rollout = tmp_path / "replacement.jsonl"
    rollout.write_text("{}\n")
    descriptor = os.open(rollout, os.O_RDONLY | os.O_CLOEXEC)
    status = os.fstat(descriptor)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]

    def walk(_root: usage_native_codex._Root) -> Iterator[tuple[str, int, os.stat_result]]:  # pyright: ignore[reportPrivateUsage]
        yield "replacement.jsonl", descriptor, status

    monkeypatch.setattr(usage_native_codex, "_walk", walk)
    try:
        before = {(status.st_dev, status.st_ino, max(0, status.st_ctime_ns - 1))}
        assert usage_native_codex._new_rollouts(root, before) == []  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_retained_pre_ready_descriptor_allows_parent_growth(tmp_path: Path) -> None:
    """A pre-READY parent rollout may append without losing its bound inode."""
    parent = tmp_path / "parent.jsonl"
    parent.write_bytes(b"{}\n")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    try:
        with parent.open("ab") as stream:
            stream.write(b"{}\n")
        usage_native_codex._reconcile_rollouts(root, before, [])  # pyright: ignore[reportPrivateUsage]
    finally:
        before.close()
        os.close(root.descriptor)


def test_native_codex_pre_ready_snapshot_rejects_same_size_generation_drift(tmp_path: Path) -> None:
    """A historical rollout cannot change generation without append-only growth."""
    parent = tmp_path / "parent.jsonl"
    parent.write_bytes(b"{}\n")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    try:
        with parent.open("ab") as stream:
            stream.write(b"x")
        parent.write_bytes(b"{}\n")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reconcile_rollouts(root, before, [])  # pyright: ignore[reportPrivateUsage]
    finally:
        before.close()
        os.close(root.descriptor)


@pytest.mark.parametrize("mutation", ["replace", "rename", "unlink"])
def test_native_codex_pre_ready_snapshot_rejects_rebinding(tmp_path: Path, mutation: str) -> None:
    """Pre-READY files cannot be replaced or disappear after READY."""
    parent = tmp_path / "parent.jsonl"
    parent.write_bytes(b"{}\n")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    try:
        if mutation == "rename":
            parent.rename(tmp_path / "renamed.jsonl")
        else:
            parent.unlink()
        if mutation == "replace":
            parent.write_bytes(b"{}\n")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reconcile_rollouts(root, before, [])  # pyright: ignore[reportPrivateUsage]
    finally:
        before.close()
        os.close(root.descriptor)


def test_native_codex_reconciliation_rejects_late_rollout(tmp_path: Path) -> None:
    """A rollout not observed in the post-READY candidate scan fails closed."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    candidate = tmp_path / "leaf.jsonl"
    candidate.write_bytes(b"{}\n")
    candidates = usage_native_codex._new_rollouts(root, before)  # pyright: ignore[reportPrivateUsage]
    try:
        (tmp_path / "late.jsonl").write_bytes(b"{}\n")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reconcile_rollouts(root, before, candidates)  # pyright: ignore[reportPrivateUsage]
    finally:
        for _name, descriptor, _status in candidates:
            os.close(descriptor)
        before.close()
        os.close(root.descriptor)


@pytest.mark.parametrize(
    ("mutation", "relaxed", "accepts"),
    [
        ("append", True, True),
        ("selected-growth", False, False),
        ("possible-child-growth", False, False),
        ("replace", True, False),
        ("rename", True, False),
        ("unlink", True, False),
        ("unsafe-mode", True, False),
        ("late", True, False),
    ],
)
def test_native_codex_reconciliation_relaxes_only_bound_unrelated_rollouts(
    tmp_path: Path, mutation: str, relaxed: bool, accepts: bool
) -> None:
    """Only a positively unrelated retained rollout may append after candidate scan."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    before = usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    rollout = tmp_path / "unrelated.jsonl"
    rollout.write_bytes(b"{}\n")
    candidates = usage_native_codex._new_rollouts(root, before)  # pyright: ignore[reportPrivateUsage]
    try:
        _name, descriptor, _status = candidates[0]
        if mutation in {"append", "selected-growth", "possible-child-growth"}:
            with rollout.open("ab") as stream:
                stream.write(b"{}\n")
        elif mutation == "replace":
            rollout.unlink()
            rollout.write_bytes(b"{}\n")
        elif mutation == "rename":
            rollout.rename(tmp_path / "renamed.jsonl")
        elif mutation == "unlink":
            rollout.unlink()
        elif mutation == "unsafe-mode":
            os.chmod(rollout, 0o666)
        else:
            (tmp_path / "late.jsonl").write_bytes(b"{}\n")
        relaxed_descriptors = frozenset({descriptor}) if relaxed else frozenset[int]()
        if accepts:
            usage_native_codex._reconcile_rollouts(  # pyright: ignore[reportPrivateUsage]
                root, before, candidates, relaxed_descriptors
            )
        else:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._reconcile_rollouts(  # pyright: ignore[reportPrivateUsage]
                    root, before, candidates, relaxed_descriptors
                )
    finally:
        for _name, descriptor, _status in candidates:
            os.close(descriptor)
        before.close()
        os.close(root.descriptor)


def test_native_codex_git_administration_tree_accepts_large_safe_nested_file(
    tmp_path: Path,
) -> None:
    """The direct administrative walk admits safe files larger than 64 KiB."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "large").write_bytes(b"x" * (65_537))
    for path in (tmp_path, nested):
        os.chmod(path, 0o755)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._assert_git_administration_tree(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize("kind", ["writable-file", "writable-dir", "symlink", "fifo"])
def test_native_codex_git_administration_tree_rejects_unsafe_nested_entry(
    tmp_path: Path, kind: str
) -> None:
    """The direct Git-admin walk rejects writable, linked, and special entries."""
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "entry"
    if kind == "writable-dir":
        target.mkdir()
        os.chmod(target, 0o775)
    elif kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"x")
        os.chmod(target, 0o664)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration_tree(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_git_administration_tree_rejects_exact_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct walk rejects entry and depth counts beyond their exact caps."""
    (tmp_path / "one").write_bytes(b"x")
    (tmp_path / "two").write_bytes(b"x")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        monkeypatch.setattr(usage_native_codex, "MAX_GIT_ADMIN_ENTRIES", 1)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration_tree(root)  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "two").unlink()
        nested = tmp_path / "nested"
        nested.mkdir()
        monkeypatch.setattr(usage_native_codex, "MAX_GIT_ADMIN_ENTRIES", 8)
        monkeypatch.setattr(usage_native_codex, "MAX_GIT_ADMIN_DEPTH", 0)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration_tree(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_git_administration_tree_rejects_stat_open_toctou(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement between no-follow stat and open is rejected by descriptor identity."""
    entry = tmp_path / "entry"
    entry.write_bytes(b"one")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    original_stat = usage_native_codex.os.stat
    changed = False

    def replace_after_stat(*args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal changed
        result = original_stat(*args, **kwargs)
        if not changed and args and args[0] == "entry":
            changed = True
            entry.unlink()
            entry.write_bytes(b"replacement")
        return result

    try:
        monkeypatch.setattr(usage_native_codex.os, "stat", replace_after_stat)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration_tree(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_walk_counts_all_provider_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty directories consume the same bounded provider-entry budget as rollouts."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_ENTRIES", 2)
        for name in ("one", "two"):
            (tmp_path / name).mkdir()

        def materialized_listdir(_path: object = None) -> NoReturn:
            pytest.fail("materialized provider traversal")

        monkeypatch.setattr(
            usage_native_codex.os,
            "listdir",
            materialized_listdir,
        )
        assert usage_native_codex._inventory(root) == set()  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "three").mkdir()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_walk_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """A provider FIFO is opened nonblocking then rejected by the closed type grammar."""
    fifo = tmp_path / "rollout.jsonl"
    os.mkfifo(fifo)
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        started = time.monotonic()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._inventory(root)  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - started < 1
    finally:
        os.close(root.descriptor)


def test_native_codex_rollout_rejects_drifting_observed_root_schema(tmp_path: Path) -> None:
    """An added 0.147.0 root payload key fails closed before recording anything."""
    rollout = tmp_path / "drift.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "ordinal": 0,
                "timestamp": "discarded",
                "type": "world_state",
                "payload": {"full": False, "state": {}, "extra": 1},
            }
        )
        + "\n"
    )
    binding = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("discriminator", [["event_msg"], {"type": "event_msg"}])
def test_native_codex_parser_rejects_non_string_root_discriminator(
    tmp_path: Path, discriminator: object
) -> None:
    """Untrusted root discriminators fail with the fixed content-free error."""
    rollout = tmp_path / "bad.jsonl"
    rollout.write_text(
        json.dumps({"ordinal": 0, "timestamp": "PATH_SECRET", "type": discriminator, "payload": {}})
        + "\n"
    )
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("value", [["token_count"], {"type": "token_count"}])
def test_native_codex_parser_rejects_non_string_nested_discriminator(
    tmp_path: Path, value: object
) -> None:
    """Nested event discriminators never reach dictionary lookup with non-text values."""
    rollout = tmp_path / "nested.jsonl"
    meta = {
        "base_instructions": "x",
        "cli_version": "0.147.0",
        "context_window": 1,
        "cwd": "x",
        "git": {"branch": "x", "commit_hash": "x", "repository_url": "x"},
        "history_mode": "x",
        "id": "root",
        "model_provider": "x",
        "originator": "codex-tui",
        "session_id": "root",
        "source": "cli",
        "thread_source": "user",
        "timestamp": "x",
    }
    rollout.write_text(
        json.dumps(
            {"ordinal": 0, "timestamp": "discarded", "type": "session_meta", "payload": meta}
        )
        + "\n"
        + json.dumps(
            {
                "ordinal": 1,
                "timestamp": "discarded",
                "type": "event_msg",
                "payload": {"type": value},
            }
        )
        + "\n"
    )
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_parser_enforces_actual_bytes_after_initial_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parser read accounting rejects data that outgrows its earlier discovery stat."""
    rollout = tmp_path / "grown.jsonl"
    rollout.write_bytes(b'{"parent-811":"/root/leaf:engineer-be"}\n')
    monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_FILE_BYTES", 1)
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(  # pyright: ignore[reportPrivateUsage]
            stream.fileno(),
            {"parent": "parent-811", "path": "/root/leaf", "role": "engineer-be"},
            "0.147.0",
        )


def test_native_codex_malformed_candidate_withholds_whole_tree_proof(
    tmp_path: Path,
) -> None:
    """A malformed first row is indeterminate rather than falsely unrelated."""
    rollout = tmp_path / "matching.jsonl"
    binding = {
        "parent_thread_id": "parent-811",
        "agent_path": "/root/leaf",
        "role": "engineer-be",
        "effort": "high",
    }
    rollout.write_text("{" + " ".join(binding.values()) + "\n")
    with rollout.open("rb") as stream:
        assert usage_native_codex._candidate_binding(stream.fileno(), binding) is None  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_parses_unrelated_root_session_without_leaf_requirements(
    tmp_path: Path,
) -> None:
    """A concurrent root rollout is schema-scanned but cannot impersonate a leaf."""
    rollout = tmp_path / "root.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "ordinal": 0,
                "timestamp": "discarded",
                "type": "session_meta",
                "payload": {
                    "base_instructions": "SECRET_PROMPT",
                    "cli_version": "0.147.0",
                    "context_window": 1,
                    "cwd": "/secret/path",
                    "git": {
                        "branch": "main",
                        "commit_hash": "a" * 40,
                        "repository_url": "SECRET_URL",
                    },
                    "history_mode": "discarded",
                    "id": "root-811",
                    "model_provider": "discarded",
                    "originator": "codex-tui",
                    "session_id": "root-811",
                    "source": "cli",
                    "thread_source": "user",
                    "timestamp": "discarded",
                },
            }
        )
        + "\n"
    )
    binding = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    with rollout.open("rb") as stream:
        session, totals, parent, matches, _consumed = usage_native_codex._parse_rollout(  # pyright: ignore[reportPrivateUsage]
            stream.fileno(), binding, "0.147.0"
        )
    assert (session, totals, parent, matches) == ("root-811", (0, 0, 0, 0, 0, 0), "", False)


@pytest.mark.parametrize(
    ("identifier", "session_id"),
    [
        ("root-811", "other-811"),
        (["root-811"], "root-811"),
        ("root-811", {"session": "root-811"}),
    ],
)
def test_native_codex_rejects_mismatched_or_nontext_session_identifiers(
    tmp_path: Path, identifier: object, session_id: object
) -> None:
    """Session metadata cannot split the recorded and topology session identities."""
    rollout = tmp_path / "bad-session.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "ordinal": 0,
                "timestamp": "discarded",
                "type": "session_meta",
                "payload": {
                    "base_instructions": "discarded",
                    "cli_version": "0.147.0",
                    "context_window": 1,
                    "cwd": "discarded",
                    "git": {
                        "branch": "main",
                        "commit_hash": "a" * 40,
                        "repository_url": "discarded",
                    },
                    "history_mode": "discarded",
                    "id": identifier,
                    "model_provider": "discarded",
                    "originator": "codex-tui",
                    "session_id": session_id,
                    "source": "cli",
                    "thread_source": "user",
                    "timestamp": "discarded",
                },
            }
        )
        + "\n"
    )
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_rejects_task_started_before_turn_context(tmp_path: Path) -> None:
    """A valid provider context becomes invalid only when it follows task start."""
    meta: dict[str, object] = {
        "type": "session_meta",
        "payload": {
            "base_instructions": "discarded",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "discarded",
            "git": {"branch": "main", "commit_hash": "a" * 40, "repository_url": "discarded"},
            "history_mode": "discarded",
            "id": "root-811",
            "model_provider": "discarded",
            "originator": "codex-tui",
            "session_id": "root-811",
            "source": "cli",
            "thread_source": "user",
            "timestamp": "discarded",
        },
    }
    context_payload: dict[str, object] = {
        key: "discarded"
        for key in usage_native_codex._TURN_CONTEXT_KEYS  # pyright: ignore[reportPrivateUsage]
    }
    context_payload.update(
        {
            "model": "gpt-5.6-terra",
            "effort": "high",
            "realtime_active": False,
            "workspace_roots": [],
        }
    )
    context: dict[str, object] = {"type": "turn_context", "payload": context_payload}
    started: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "collaboration_mode_kind": "discarded",
            "model_context_window": 1,
            "started_at": "discarded",
            "turn_id": "discarded",
        },
    }

    def write(name: str, events: tuple[dict[str, object], ...]) -> Path:
        rollout = tmp_path / name
        rollout.write_text(
            "".join(
                json.dumps({"ordinal": ordinal, "timestamp": "discarded", **event}) + "\n"
                for ordinal, event in enumerate(events)
            )
        )
        return rollout

    ordered = write("ordered.jsonl", (meta, context, started))
    with ordered.open("rb") as stream:
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]
    malformed = write("context-after-start.jsonl", (meta, started, context))
    with (
        malformed.open("rb") as stream,
        pytest.raises(usage_native_codex.NativeCodexCaptureError),
    ):
        usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_rejects_out_of_order_or_repeated_task_events(tmp_path: Path) -> None:
    """A fully scanned rollout cannot reorder or repeat task lifecycle markers."""
    meta = {
        "type": "session_meta",
        "payload": {
            "base_instructions": "discarded",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "discarded",
            "git": {"branch": "main", "commit_hash": "a" * 40, "repository_url": "discarded"},
            "history_mode": "discarded",
            "id": "root-811",
            "model_provider": "discarded",
            "originator": "codex-tui",
            "session_id": "root-811",
            "source": "cli",
            "thread_source": "user",
            "timestamp": "discarded",
        },
    }
    started: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "collaboration_mode_kind": "discarded",
            "model_context_window": 1,
            "started_at": "discarded",
            "turn_id": "discarded",
        },
    }
    complete = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "completed_at": "discarded",
            "duration_ms": 1,
            "last_agent_message": "discarded",
            "started_at": "discarded",
            "time_to_first_token_ms": 1,
            "turn_id": "discarded",
        },
    }
    binding = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    for index, events in enumerate(((meta, complete), (meta, started, started)), 1):
        rollout = tmp_path / f"ordered-{index}.jsonl"
        rollout.write_text(
            "".join(
                json.dumps({"ordinal": ordinal, "timestamp": "discarded", **event}) + "\n"
                for ordinal, event in enumerate(events)
            )
        )
        with (
            rollout.open("rb") as stream,
            pytest.raises(usage_native_codex.NativeCodexCaptureError),
        ):
            usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("position", "accepts"),
    [("within", True), ("before-start", False), ("after-complete", False)],
)
def test_native_codex_response_items_require_active_task_window(
    tmp_path: Path, position: str, accepts: bool
) -> None:
    """Response items are admitted only between task start and completion."""
    meta: dict[str, object] = {
        "type": "session_meta",
        "payload": {
            "base_instructions": "discarded",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "discarded",
            "git": {"branch": "main", "commit_hash": "a" * 40, "repository_url": "discarded"},
            "history_mode": "discarded",
            "id": "root-811",
            "model_provider": "discarded",
            "originator": "codex-tui",
            "session_id": "root-811",
            "source": "cli",
            "thread_source": "user",
            "timestamp": "discarded",
        },
    }
    context_payload: dict[str, object] = {
        key: "discarded"
        for key in usage_native_codex._TURN_CONTEXT_KEYS  # pyright: ignore[reportPrivateUsage]
    }
    context_payload.update(
        {
            "model": "gpt-5.6-terra",
            "effort": "high",
            "realtime_active": False,
            "workspace_roots": [],
            "turn_id": "turn-811",
        }
    )
    context: dict[str, object] = {"type": "turn_context", "payload": context_payload}
    started: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "collaboration_mode_kind": "discarded",
            "model_context_window": 1,
            "started_at": "discarded",
            "turn_id": "turn-811",
        },
    }
    totals: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 0,
                },
                "model_context_window": 1,
                "total_token_usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 2,
                },
            },
            "rate_limits": {},
        },
    }
    complete: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "completed_at": "discarded",
            "duration_ms": 1,
            "last_agent_message": "discarded",
            "started_at": "discarded",
            "time_to_first_token_ms": 1,
            "turn_id": "turn-811",
        },
    }
    response: dict[str, object] = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "content": "discarded",
            "id": "discarded",
            "internal_chat_message_metadata_passthrough": {},
            "role": "assistant",
        },
    }
    event_options: dict[str, tuple[dict[str, object], ...]] = {
        "within": (meta, context, started, response, totals, complete),
        "before-start": (meta, context, response, started, totals, complete),
        "after-complete": (meta, context, started, totals, complete, response),
    }
    events = event_options[position]
    rollout = tmp_path / f"{position}.jsonl"
    rollout.write_text(
        "".join(
            json.dumps({"ordinal": ordinal, "timestamp": "discarded", **event}) + "\n"
            for ordinal, event in enumerate(events)
        )
    )
    with rollout.open("rb") as stream:
        if accepts:
            usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._parse_rollout(stream.fileno(), {}, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_rejects_decreasing_cumulative_token_totals(tmp_path: Path) -> None:
    """A later cumulative token snapshot may never decrease any token category."""
    binding = {
        "parent_thread_id": "parent",
        "role": "engineer-be",
        "agent_path": "/root/leaf",
        "effort": "high",
    }
    meta = {
        "type": "session_meta",
        "payload": {
            "id": "leaf",
            "agent_nickname": "nick",
            "agent_path": "/root/leaf",
            "agent_role": "engineer-be",
            "base_instructions": "x",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "x",
            "git": {"branch": "x", "commit_hash": "a", "repository_url": "x"},
            "history_mode": "x",
            "model_provider": "openai",
            "multi_agent_version": "v2",
            "originator": "codex-tui",
            "parent_thread_id": "parent",
            "session_id": "leaf",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent",
                        "depth": 1,
                        "agent_path": "/root/leaf",
                        "agent_nickname": "nick",
                        "agent_role": "engineer-be",
                    }
                }
            },
            "thread_source": "subagent",
            "timestamp": "x",
        },
    }
    context_payload: dict[str, object] = {
        key: "x"
        for key in usage_native_codex._TURN_CONTEXT_KEYS  # pyright: ignore[reportPrivateUsage]
    }
    context_payload.update(
        {
            "model": "gpt-5.6-terra",
            "effort": "high",
            "multi_agent_version": "v2",
            "realtime_active": False,
            "workspace_roots": [],
        }
    )
    context: dict[str, object] = {"type": "turn_context", "payload": context_payload}

    def totals(value: int) -> dict[str, object]:
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {},
                "info": {
                    "last_token_usage": {
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 0,
                    },
                    "model_context_window": 1,
                    "total_token_usage": {
                        "input_tokens": value,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 1,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 1,
                        "total_tokens": value + 1,
                    },
                },
            },
        }

    started = {
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "collaboration_mode_kind": "x",
            "model_context_window": 1,
            "started_at": "x",
            "turn_id": "x",
        },
    }
    events = [meta, context, started, totals(2), totals(1)]
    unknown_event = totals(2)
    unknown_event["payload"] = {**unknown_event["payload"], "type": "unknown"}  # type: ignore[index]
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(
        "".join(
            json.dumps({"ordinal": index, "timestamp": "x", **event}) + "\n"
            for index, event in enumerate([meta, context, started, unknown_event])
        )
    )
    with unknown.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]
    rollout = tmp_path / "decreasing.jsonl"
    rollout.write_text(
        "".join(
            json.dumps({"ordinal": index, "timestamp": "x", **event}) + "\n"
            for index, event in enumerate(events)
        )
    )
    with rollout.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_streams_two_mebibyte_content_event_at_exact_boundary(tmp_path: Path) -> None:
    """A realistic 0.147.0 content-bearing response is parsed and discarded at the cap."""
    root_meta = {
        "ordinal": 0,
        "timestamp": "discarded",
        "type": "session_meta",
        "payload": {
            "base_instructions": "discarded",
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": "discarded",
            "git": {"branch": "main", "commit_hash": "a" * 40, "repository_url": "discarded"},
            "history_mode": "discarded",
            "id": "root-811",
            "model_provider": "discarded",
            "originator": "codex-tui",
            "session_id": "root-811",
            "source": "cli",
            "thread_source": "user",
            "timestamp": "discarded",
        },
    }
    context_payload: dict[str, object] = {
        key: "discarded"
        for key in usage_native_codex._TURN_CONTEXT_KEYS  # pyright: ignore[reportPrivateUsage]
    }
    context_payload.update(
        {
            "model": "gpt-5.6-terra",
            "effort": "high",
            "realtime_active": False,
            "workspace_roots": [],
        }
    )
    context: dict[str, object] = {
        "ordinal": 1,
        "timestamp": "discarded",
        "type": "turn_context",
        "payload": context_payload,
    }
    started: dict[str, object] = {
        "ordinal": 2,
        "timestamp": "discarded",
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "collaboration_mode_kind": "discarded",
            "model_context_window": 1,
            "started_at": "discarded",
            "turn_id": "discarded",
        },
    }
    response_payload: dict[str, object] = {
        "type": "message",
        "content": "",
        "id": "discarded",
        "internal_chat_message_metadata_passthrough": {},
        "role": "assistant",
    }
    response: dict[str, object] = {
        "ordinal": 3,
        "timestamp": "discarded",
        "type": "response_item",
        "payload": response_payload,
    }
    empty = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    response_payload["content"] = "S" * (usage_native_codex.MAX_EVENT_BYTES - len(empty))
    exact = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    assert len(exact) == usage_native_codex.MAX_EVENT_BYTES
    binding = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    rollout = tmp_path / "large.jsonl"
    preamble = b"".join(
        json.dumps(event, separators=(",", ":")).encode() + b"\n"
        for event in (root_meta, context, started)
    )
    rollout.write_bytes(preamble + exact)
    with rollout.open("rb") as stream:
        _session, totals, _parent, matches, consumed = usage_native_codex._parse_rollout(  # pyright: ignore[reportPrivateUsage]
            stream.fileno(), binding, "0.147.0"
        )
    assert totals == (0, 0, 0, 0, 0, 0) and matches is False
    assert consumed == rollout.stat().st_size
    rollout.write_bytes(b"")
    assert consumed > rollout.stat().st_size
    rollout.write_bytes(b"G" * (consumed + 1))
    assert consumed < rollout.stat().st_size

    one_over = tmp_path / "one-over.jsonl"
    one_over.write_bytes(preamble + exact[:-1] + b"S\n")
    with one_over.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_bounded_reader_rejects_partial_and_count_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded reader rejects an unterminated final event and count excess pre-parse."""
    binding = {
        "parent_thread_id": "parent-811",
        "role": "engineer-be",
        "agent_path": "/root/native-codex-capture",
        "effort": "high",
    }
    partial = tmp_path / "partial.jsonl"
    partial.write_bytes(b'{"ordinal":0}')
    with partial.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_LINES", 1)
    overflow = tmp_path / "overflow.jsonl"
    overflow.write_bytes(b"{}\n{}\n")
    with overflow.open("rb") as stream, pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._parse_rollout(stream.fileno(), binding, "0.147.0")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_json_depth_fails_closed() -> None:
    """Deep JSON maps to the fixed capture error without recursive parsing."""
    deep = (b"[" * (usage_native_codex.MAX_JSON_NESTING + 1)) + (
        b"]" * (usage_native_codex.MAX_JSON_NESTING + 1)
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._json(deep)  # pyright: ignore[reportPrivateUsage]


def test_native_codex_json_recursion_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser recursion failure maps to the fixed metadata-safe error."""

    def recurse(*_args: object, **_kwargs: object) -> NoReturn:
        raise RecursionError

    monkeypatch.setattr(usage_native_codex.json, "loads", recurse)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._json(b"{}")  # pyright: ignore[reportPrivateUsage]


def test_native_codex_cli_maps_nested_discriminator_error_without_leakage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Supervisor parser failures reach the CLI as one fixed metadata-safe error."""

    def fail(_arguments: object, *, harness_version: str) -> NoReturn:
        assert harness_version == "0.147.0"
        raise usage_native_codex.NativeCodexCaptureError("/host/SECRET trace")

    def resolved(_family: HarnessFamily) -> str:
        return "/stub/codex"

    def version(_executable: str) -> str:
        return "0.147.0"

    monkeypatch.setattr(usage_cli, "_resolved_executable", resolved)
    monkeypatch.setattr(usage_cli, "_harness_version", version)
    monkeypatch.setattr(usage_cli, "supervise_native_codex", fail)
    with pytest.raises(SystemExit, match="native Codex capture is invalid") as error:
        main(
            [
                "supervise-native-codex",
                "--role",
                "engineer-be",
                "--task-id",
                "x",
                "--slice-id",
                "x",
                "--parent-task-id",
                "x",
                "--task-name",
                "x",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "main",
                "--base-sha",
                "a" * 40,
                "--usage-root",
                "/tmp/x",
            ]
        )
    assert "SECRET" not in str(error.value) and "Traceback" not in str(error.value)
    assert capsys.readouterr().err == ""


def test_native_codex_launch_rejects_ignored_only_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch identity includes ignored files, while final attestation remains ordinary clean."""
    values = iter(
        ("https://github.com/syamaner/roastpilot-agent.git", "a" * 40, "main", "ignored-file")
    )

    def git(_args: list[str]) -> str:
        return next(values)

    monkeypatch.setattr(usage_native_codex, "_git", git)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._git_identity("syamaner/roastpilot-agent", "main", "a" * 40, final=False)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("final", [False, True])
def test_native_codex_git_identity_forces_all_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final: bool
) -> None:
    """A local status setting cannot hide untracked material from either attestation."""
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "capture@example.test"],
        ["git", "config", "user.name", "Capture"],
        ["git", "remote", "add", "origin", "https://github.com/syamaner/roastpilot-agent.git"],
    ):
        subprocess.run(arguments, cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "config", "status.showUntrackedFiles", "no"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    (repository / "hidden-untracked.txt").write_text("must be visible\n")
    monkeypatch.chdir(repository)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._git_identity(  # pyright: ignore[reportPrivateUsage]
            "syamaner/roastpilot-agent", "main", base, final=final
        )


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_native_codex_index_attestation_rejects_hidden_tracked_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """Assume-unchanged and skip-worktree entries cannot hide checkout changes."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "capture@example.test"], cwd=repository, check=True
    )
    subprocess.run(["git", "config", "user.name", "Capture"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True
    )
    monkeypatch.chdir(repository)

    usage_native_codex._assert_index_clean()  # pyright: ignore[reportPrivateUsage]
    subprocess.run(["git", "update-index", flag, "tracked.txt"], cwd=repository, check=True)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._assert_index_clean()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("target", ["file", "directory"])
def test_native_codex_checkout_attestation_rejects_writable_tracked_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Tracked files and nested directories remain owned and non-writable at handback."""
    repository = tmp_path / "repository"
    nested = repository / "nested"
    nested.mkdir(parents=True)
    tracked = nested / "tracked.txt"
    tracked.write_text("tracked\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "add", "nested/tracked.txt"], cwd=repository, check=True)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
        os.chmod(tracked if target == "file" else nested, 0o664 if target == "file" else 0o775)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize("kind", ["ignored", "untracked"])
def test_native_codex_checkout_attestation_covers_empty_untracked_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Empty ignored and untracked directories receive the same descriptor-safe check."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    if kind == "ignored":
        (repository / ".gitignore").write_text("ignored/\n")
    nested = repository / kind / "empty" / "nested"
    nested.mkdir(parents=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
        os.chmod(nested, 0o775)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_directory_walk_bounds_all_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout directory walk counts untracked directory entries too."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    (repository / "untracked").mkdir()
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    monkeypatch.setattr(usage_native_codex, "MAX_CHECKOUT_ENTRIES", 1)
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_directory_walk_admits_dependency_scale_entries(
    tmp_path: Path,
) -> None:
    """The checkout walk admits a dependency tree above the former 4,096-entry cap."""
    generated = tmp_path / "generated"
    generated.mkdir()
    for index in range(4097):
        (generated / f"entry-{index}").touch()
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_directory_walk_entry_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded checkout walk accepts its exact entry cap and rejects one over."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(usage_native_codex, "MAX_CHECKOUT_ENTRIES", 2)
    try:
        (tmp_path / "one").touch()
        (tmp_path / "two").touch()
        usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "three").touch()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_directory_walk_depth_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout walk accepts its exact depth cap and rejects one nested level over."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(usage_native_codex, "MAX_CHECKOUT_DEPTH", 2)
    try:
        exact = tmp_path / "one" / "two"
        exact.mkdir(parents=True)
        usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
        (exact / "three").mkdir()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_attestation_accepts_standard_leaf_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standard leaf-owned .venv may contain interpreter symlinks after READY."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    previous_umask = os.umask(0o022)
    try:
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(repository / ".venv")
    finally:
        os.umask(previous_umask)
    venv_root = repository / ".venv"
    for directory, directories, files in os.walk(venv_root, followlinks=False):
        for name in (".", *directories, *files):
            path = Path(directory) / name
            status = path.lstat()
            if status.st_uid != os.geteuid():
                pytest.fail("venv fixture contains an unowned node")
            if stat.S_ISDIR(status.st_mode):
                os.chmod(path, 0o755, follow_symlinks=False)
            elif stat.S_ISREG(status.st_mode):
                os.chmod(path, 0o644, follow_symlinks=False)
    absolute_interpreter = next(
        (
            path
            for path in (venv_root / "bin").iterdir()
            if path.is_symlink()
            and re.fullmatch(r"python(?:3(?:\.\d+)?)?", path.name)
            and os.path.isabs(os.readlink(path))
        ),
        None,
    )
    if absolute_interpreter is None:
        pytest.fail("standard venv lacks an absolute interpreter symlink")
    interpreter_target = tmp_path / f"python{sys.version_info.major}.{sys.version_info.minor}"
    interpreter_target.write_bytes(b"fixture interpreter\n")
    interpreter_target.chmod(0o755)
    absolute_interpreter.unlink()
    absolute_interpreter.symlink_to(interpreter_target)
    monkeypatch.setattr(
        usage_native_codex.sys, "_base_executable", str(interpreter_target), raising=False
    )
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
        venv_config = repository / ".venv" / "pyvenv.cfg"
        os.chmod(venv_config, 0o664)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
        os.chmod(venv_config, 0o644)
        os.mkfifo(repository / ".venv" / "unsafe")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize(
    ("target_kind", "mode", "link_name", "version_info", "encoding", "accepts"),
    [
        ("trusted", 0o755, "python3.11", None, None, True),
        ("trusted", 0o755, "𝜋thon", (3, 14, 4), "utf-8", True),
        ("other", 0o755, "python3.11", None, None, False),
        ("trusted", 0o775, "python3.11", None, None, False),
        ("trusted", 0o757, "python3.11", None, None, False),
        ("missing", 0o755, "python3.11", None, None, False),
    ],
)
def test_native_codex_checkout_attestation_binds_absolute_venv_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
    mode: int,
    link_name: str,
    version_info: tuple[int, int, int] | None,
    encoding: str | None,
    accepts: bool,
) -> None:
    """Absolute venv interpreter links bind only to the trusted base executable inode."""
    repository = tmp_path / "repository"
    (repository / ".venv" / "bin").mkdir(parents=True)
    trusted = tmp_path / "python3.14"
    trusted.write_text("#!/bin/sh\n")
    trusted.chmod(mode)
    other = tmp_path / "python3.13"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    target = {
        "trusted": str(trusted),
        "other": str(other),
        "missing": str(tmp_path / "python3.12"),
    }[target_kind]
    (repository / ".venv" / "bin" / link_name).symlink_to(target)
    monkeypatch.setattr(usage_native_codex.sys, "_base_executable", str(trusted), raising=False)
    if version_info is not None and encoding is not None:
        monkeypatch.setattr(usage_native_codex.sys, "version_info", version_info)
        monkeypatch.setattr(usage_native_codex.sys, "getfilesystemencoding", lambda: encoding)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        if accepts:
            usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize(
    ("link_name", "version_info", "encoding", "accepts"),
    [
        ("𝜋thon", (3, 14, 4), "utf-8", True),
        ("𝜋thon", (3, 11, 14), "utf-8", False),
        ("𝜋thon", (3, 14, 4), "ascii", False),
        ("πthon", (3, 14, 4), "utf-8", False),
        ("𝜋thon3", (3, 14, 4), "utf-8", False),
        ("python𝜋", (3, 14, 4), "utf-8", False),
    ],
)
def test_native_codex_checkout_attestation_admits_only_exact_unicode_venv_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_name: str,
    version_info: tuple[int, int, int],
    encoding: str,
    accepts: bool,
) -> None:
    """Only Python 3.14's exact ``𝜋thon`` interpreter alias is admitted."""
    repository = tmp_path / "repository"
    interpreter = repository / ".venv" / "bin" / "python3.14"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    (interpreter.parent / link_name).symlink_to("python3.14")
    monkeypatch.setattr(usage_native_codex.sys, "version_info", version_info)
    monkeypatch.setattr(usage_native_codex.sys, "getfilesystemencoding", lambda: encoding)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        if accepts:
            usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.skipif(
    os.name == "nt" or sys.version_info[:2] != (3, 14) or sys.getfilesystemencoding() != "utf-8",
    reason="requires the Python 3.14 POSIX venv layout",
)
def test_native_codex_checkout_attestation_accepts_current_python314_venv_alias(
    tmp_path: Path,
) -> None:
    """The current Python 3.14 POSIX venv's emitted ``𝜋thon`` alias remains accepted."""
    repository = tmp_path / "repository"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(repository / ".venv")
    alias = repository / ".venv" / "bin" / "𝜋thon"
    if not alias.is_symlink():
        pytest.skip("Python 3.14 venv did not emit the UTF-8 interpreter alias")
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._assert_checkout_directories(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize(
    ("top_parent", "nested_parent"),
    [("leaf-session", "other-session"), ("other-session", "leaf-session")],
)
def test_native_codex_candidate_metadata_withholds_contradictory_parents(
    tmp_path: Path, top_parent: str, nested_parent: str
) -> None:
    """Conflicting top-level and spawn parents remain indeterminate topology evidence."""
    rollout = tmp_path / "candidate.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "ordinal": 0,
                "timestamp": "2026-08-20T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "parent_thread_id": top_parent,
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": nested_parent,
                                "agent_path": "/root/task",
                                "agent_role": "engineer-be",
                                "depth": 1,
                            }
                        }
                    },
                },
            }
        )
        + "\n"
    )
    descriptor = os.open(rollout, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = usage_native_codex._candidate_metadata(  # pyright: ignore[reportPrivateUsage]
            descriptor,
            {"parent_thread_id": "parent", "agent_path": "/root/task", "role": "engineer-be"},
        )
    finally:
        os.close(descriptor)
    assert metadata.bound_leaf is None
    assert metadata.parent_thread_id is None


def _native_codex_npm_bin_repository(
    tmp_path: Path,
    *,
    link_path: str,
    target: str,
    target_path: str | None,
    target_kind: str,
) -> Path:
    """Create one real checkout with a synthetic npm executable link."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    if target_path is not None:
        executable = repository / target_path
        executable.parent.mkdir(parents=True)
        if target_kind == "symlink":
            executable.symlink_to("actual.js")
        elif target_kind == "directory":
            executable.mkdir()
        elif target_kind == "fifo":
            os.mkfifo(executable)
        else:
            executable.write_text("#!/usr/bin/env node\n")
            if target_kind == "writable":
                os.chmod(executable, 0o664)
    link = repository / link_path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return repository


@pytest.mark.parametrize(
    ("link_path", "target", "target_path", "target_kind", "accepted"),
    [
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "regular",
            True,
            id="nested-valid",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "/outside",
            None,
            "regular",
            False,
            id="absolute-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../missing/bin/semver.js",
            None,
            "regular",
            False,
            id="missing-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "symlink",
            False,
            id="symlink-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "writable",
            False,
            id="writable-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "directory",
            False,
            id="directory-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "fifo",
            False,
            id="fifo-target",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/not-bin-semver",
            "semver/bin/semver.js",
            "web/node_modules/pkg/node_modules/semver/bin/semver.js",
            "regular",
            False,
            id="link-outside-bin",
        ),
        pytest.param(
            "node_modules/.bin/semver",
            "../semver/bin/semver.js",
            "node_modules/semver/bin/semver.js",
            "regular",
            False,
            id="bin-outside-web-node-modules",
        ),
        pytest.param(
            "web/node_modules/pkg/node_modules/.bin/semver",
            "../../../../../outside",
            None,
            "regular",
            False,
            id="lexical-escape",
        ),
    ],
)
def test_native_codex_checkout_attestation_npm_bin_symlink_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_path: str,
    target: str,
    target_path: str | None,
    target_kind: str,
    accepted: bool,
) -> None:
    """Only descriptor-safe npm executable links within web node_modules are admissible."""
    repository = _native_codex_npm_bin_repository(
        tmp_path,
        link_path=link_path,
        target=target,
        target_path=target_path,
        target_kind=target_kind,
    )
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        if accepted:
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_attestation_rejects_group_writable_non_venv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venv-only group-write exception never widens ordinary checkout files."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    os.chmod(tracked, 0o664)
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_checkout_attestation_rejects_unsafe_non_venv_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only inert interpreter links inside .venv receive the narrow exception."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    (repository / "unsafe").symlink_to("/tmp")
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(repository)
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_checkout(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_git_administration_accepts_normal_and_linked_worktrees(
    tmp_path: Path,
) -> None:
    """Normal and linked-worktree Git layouts retain safe held identities."""
    normal = tmp_path / "normal"
    normal.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=normal, check=True, capture_output=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    git_directory = tmp_path / "git-directory"
    common_directory = tmp_path / "common-directory"
    git_directory.mkdir()
    common_directory.mkdir()
    (linked / ".git").write_text(f"gitdir: {git_directory}\n")
    common_relative = os.path.relpath(common_directory, git_directory)
    (git_directory / "commondir").write_text(f"{common_relative}\n")

    for worktree in (normal, linked):
        root = usage_native_codex._open_root(str(worktree), private=False)  # pyright: ignore[reportPrivateUsage]
        administration: usage_native_codex._GitAdministration | None = None  # pyright: ignore[reportPrivateUsage]
        try:
            administration = usage_native_codex._open_git_administration(root)  # pyright: ignore[reportPrivateUsage]
            usage_native_codex._assert_git_administration(root, administration)  # pyright: ignore[reportPrivateUsage]
        finally:
            if administration is not None:
                usage_native_codex._close_git_administration(administration)  # pyright: ignore[reportPrivateUsage]
            os.close(root.descriptor)


def test_native_codex_git_administration_retries_transient_shared_common_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent linked-worktree update must settle before shared admin passes."""
    git_directory = usage_native_codex._Root(10, 1, 1)  # pyright: ignore[reportPrivateUsage]
    common_directory = usage_native_codex._Root(11, 1, 2)  # pyright: ignore[reportPrivateUsage]
    administration = usage_native_codex._GitAdministration(  # pyright: ignore[reportPrivateUsage]
        None, git_directory, None, common_directory
    )
    common_attempts = 0

    def scan(root: usage_native_codex._Root) -> None:  # pyright: ignore[reportPrivateUsage]
        nonlocal common_attempts
        if root == common_directory:
            common_attempts += 1
            if common_attempts == 1:
                raise usage_native_codex.NativeCodexCaptureError("transient")

    monkeypatch.setattr(usage_native_codex, "_assert_git_administration_tree", scan)
    usage_native_codex._assert_git_administration_trees(administration)  # pyright: ignore[reportPrivateUsage]
    assert common_attempts == 3


def test_native_codex_git_administration_rejects_persistent_shared_common_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared admin tree which never stabilizes remains fail-closed."""
    git_directory = usage_native_codex._Root(10, 1, 1)  # pyright: ignore[reportPrivateUsage]
    common_directory = usage_native_codex._Root(11, 1, 2)  # pyright: ignore[reportPrivateUsage]
    administration = usage_native_codex._GitAdministration(  # pyright: ignore[reportPrivateUsage]
        None, git_directory, None, common_directory
    )
    common_attempts = 0

    def scan(root: usage_native_codex._Root) -> None:  # pyright: ignore[reportPrivateUsage]
        nonlocal common_attempts
        if root == common_directory:
            common_attempts += 1
            raise usage_native_codex.NativeCodexCaptureError("persistent")

    monkeypatch.setattr(usage_native_codex, "_assert_git_administration_tree", scan)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._assert_git_administration_trees(administration)  # pyright: ignore[reportPrivateUsage]
    assert common_attempts == usage_native_codex.GIT_ADMIN_SHARED_SCAN_ATTEMPTS  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("mutation", ["git-directory-mode", "git-directory-replace"])
def test_native_codex_git_administration_rejects_normal_layout_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Normal .git directories cannot become writable or be replaced after launch."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, capture_output=True)
    root = usage_native_codex._open_root(str(worktree), private=False)  # pyright: ignore[reportPrivateUsage]
    administration = usage_native_codex._open_git_administration(root)  # pyright: ignore[reportPrivateUsage]
    try:
        dotgit = worktree / ".git"
        if mutation == "git-directory-mode":
            os.chmod(dotgit, 0o775)
        else:
            os.rename(dotgit, tmp_path / "old-git")
            dotgit.mkdir()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration(root, administration)  # pyright: ignore[reportPrivateUsage]
    finally:
        usage_native_codex._close_git_administration(administration)  # pyright: ignore[reportPrivateUsage]
        os.close(root.descriptor)


@pytest.mark.parametrize("mutation", ["dotgit-mode", "common-directory-mode", "dotgit-drift"])
def test_native_codex_git_administration_rejects_linked_worktree_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Linked .git pointers and common directories remain owned and immutable."""
    worktree = tmp_path / "worktree"
    git_directory = tmp_path / "git-directory"
    common_directory = tmp_path / "common-directory"
    worktree.mkdir()
    git_directory.mkdir()
    common_directory.mkdir()
    dotgit = worktree / ".git"
    dotgit.write_text(f"gitdir: {git_directory}\n")
    common_relative = os.path.relpath(common_directory, git_directory)
    (git_directory / "commondir").write_text(f"{common_relative}\n")
    root = usage_native_codex._open_root(str(worktree), private=False)  # pyright: ignore[reportPrivateUsage]
    administration = usage_native_codex._open_git_administration(root)  # pyright: ignore[reportPrivateUsage]
    try:
        if mutation == "dotgit-mode":
            os.chmod(dotgit, 0o664)
        elif mutation == "common-directory-mode":
            os.chmod(common_directory, 0o775)
        else:
            dotgit.write_text(f"gitdir: {tmp_path / 'other'}\n")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_git_administration(root, administration)  # pyright: ignore[reportPrivateUsage]
    finally:
        usage_native_codex._close_git_administration(administration)  # pyright: ignore[reportPrivateUsage]
        os.close(root.descriptor)


@pytest.mark.parametrize("mutation", ["grow", "truncate", "replace", "unlink"])
def test_native_codex_selected_rollout_requires_unchanged_generation(
    tmp_path: Path, mutation: str
) -> None:
    """Selected provider evidence cannot change after parse while capture is finalizing."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "leaf.jsonl"
    rollout.write_bytes(b"{}\n")
    root = usage_native_codex._open_root(str(sessions), private=False)  # pyright: ignore[reportPrivateUsage]
    descriptor = os.open(rollout, os.O_RDONLY | os.O_CLOEXEC)
    status = os.fstat(descriptor)
    generation = usage_native_codex._generation(status)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._assert_selected_rollout(  # pyright: ignore[reportPrivateUsage]
            root,
            descriptor,
            generation,
            status.st_size,
            {generation},
        )
        if mutation == "grow":
            rollout.write_bytes(b"{}\n{}\n")
        elif mutation == "truncate":
            rollout.write_bytes(b"")
        elif mutation == "replace":
            rollout.unlink()
            rollout.write_bytes(b"{}\n")
        else:
            rollout.unlink()
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_selected_rollout(  # pyright: ignore[reportPrivateUsage]
                root,
                descriptor,
                generation,
                status.st_size,
                {generation},
            )
    finally:
        os.close(descriptor)
        os.close(root.descriptor)


def test_native_codex_cli_round_trip_and_fixed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closed supervisor CLI grammar maps capture failures to one safe exit."""
    arguments = usage_cli.build_parser().parse_args(
        [
            "supervise-native-codex",
            "--role",
            "engineer-be",
            "--task-id",
            "task",
            "--slice-id",
            "slice",
            "--parent-task-id",
            "parent",
            "--task-name",
            "leaf",
            "--repository",
            "syamaner/roastpilot-agent",
            "--branch",
            "feature/x",
            "--base-sha",
            "a" * 40,
            "--usage-root",
            "/private/tmp/usage",
        ]
    )
    assert arguments.role == "engineer-be" and arguments.task_name == "leaf"

    observed: list[str] = []

    def resolved(family: HarnessFamily) -> str:
        assert family is HarnessFamily.CODEX
        return "/stub/codex"

    def version(executable: str) -> str:
        observed.append(executable)
        return "0.147.0"

    def fail(_arguments: argparse.Namespace, *, harness_version: str) -> int:
        assert harness_version == "0.147.0"
        raise usage_native_codex.NativeCodexCaptureError("SECRET")

    monkeypatch.setattr(usage_cli, "_resolved_executable", resolved)
    monkeypatch.setattr(usage_cli, "_harness_version", version)
    monkeypatch.setattr(usage_cli, "supervise_native_codex", fail)
    with pytest.raises(SystemExit) as error:
        usage_cli.main(
            [
                "supervise-native-codex",
                "--role",
                "engineer-be",
                "--task-id",
                "task",
                "--slice-id",
                "slice",
                "--parent-task-id",
                "parent",
                "--task-name",
                "leaf",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/x",
                "--base-sha",
                "a" * 40,
                "--usage-root",
                "/private/tmp/usage",
            ]
        )
    assert observed == ["/stub/codex"]
    assert error.value.code == "capture-agent-usage: native Codex capture is invalid"


def test_native_codex_cli_probes_resolved_executable_once_before_supervision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper owns exactly one fixed version probe and passes it inward."""

    class VersionProcess:
        """Completed version probe exposing only the subprocess contract in use."""

        def __init__(self) -> None:
            self.stdout = BytesIO(b"codex 0.148.0\n")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed version probe must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed version probe must not kill")

    probe_calls: list[tuple[list[str], dict[str, object]]] = []
    observed_versions: list[str] = []

    def popen(argv: list[str], **kwargs: object) -> VersionProcess:
        probe_calls.append((argv, kwargs))
        return VersionProcess()

    def resolved(family: HarnessFamily) -> str:
        assert family is HarnessFamily.CODEX
        return "/fixed/codex"

    def supervise(_arguments: object, *, harness_version: str) -> int:
        observed_versions.append(harness_version)
        return 0

    monkeypatch.setattr(usage_cli, "_resolved_executable", resolved)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", popen)
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)
    monkeypatch.setattr(usage_cli, "supervise_native_codex", supervise)
    monkeypatch.setenv("PATH", "/allowed/codex-bin")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/host/injected.js")
    monkeypatch.setenv("ROASTPILOT_PROBE_SENTINEL", "hostile")
    monkeypatch.setenv("roastpilot_probe_sentinel", "hostile")

    assert usage_cli.supervise_native_codex_command(argparse.Namespace()) == 0
    assert [argv for argv, _kwargs in probe_calls] == [["/fixed/codex", "--version"]]
    probe_environment = cast(dict[str, str], probe_calls[0][1]["env"])
    assert probe_environment["PATH"] == "/allowed/codex-bin"
    assert probe_environment["LANG"] == "en_GB.UTF-8"
    assert "NODE_OPTIONS" not in probe_environment
    assert "ROASTPILOT_PROBE_SENTINEL" not in probe_environment
    assert "roastpilot_probe_sentinel" not in probe_environment
    assert set(probe_environment) <= usage_cli._NATIVE_SAFE_INHERITED_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    assert observed_versions == ["0.148.0"]


def test_native_codex_walk_rejects_symlink_non_json_and_depth(tmp_path: Path) -> None:
    """Provider traversal accepts only bounded-depth owned JSONL regular files."""
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        (tmp_path / "bad.txt").write_text("x")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            list(usage_native_codex._walk(root))  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "bad.txt").unlink()
        (tmp_path / "target.jsonl").write_text("x")
        (tmp_path / "link.jsonl").symlink_to(tmp_path / "target.jsonl")
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            list(usage_native_codex._walk(root))  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


def test_native_codex_walk_rejects_over_depth_tree(tmp_path: Path) -> None:
    """Traversal closes and rejects trees deeper than the closed provider bound."""
    current = tmp_path
    for index in range(usage_native_codex.MAX_PROVIDER_DEPTH + 1):
        current = current / f"d{index}"
        current.mkdir()
    (current / "rollout.jsonl").write_text("x")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            list(usage_native_codex._walk(root))  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(root.descriptor)


@pytest.mark.parametrize("invalid", [False, True])
def test_native_codex_walk_explicitly_closes_scandir_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    """Nested traversal closes every descriptor duplicated for scandir, even on failure."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ("invalid.txt" if invalid else "rollout.jsonl")).write_text("{}\n")
    root = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    scanned: list[int] = []
    closed: list[int] = []
    real_scandir = os.scandir
    real_close = os.close

    def scandir(descriptor: int) -> Any:
        scanned.append(descriptor)
        return cast(Any, real_scandir(descriptor))

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(usage_native_codex.os, "scandir", scandir)
    monkeypatch.setattr(usage_native_codex, "_close", close)
    try:
        if invalid:
            with pytest.raises(usage_native_codex.NativeCodexCaptureError):
                list(usage_native_codex._walk(root))  # pyright: ignore[reportPrivateUsage]
        else:
            rollouts = list(usage_native_codex._walk(root))  # pyright: ignore[reportPrivateUsage]
            for _name, descriptor, _status in rollouts:
                os.close(descriptor)
    finally:
        os.close(root.descriptor)
    assert len(scanned) == 2
    assert all(descriptor in closed for descriptor in scanned)


@pytest.mark.parametrize(
    ("repository", "branch", "base", "responses", "accepts"),
    [
        ("wrong/repo", "feature/x", "a" * 40, [], False),
        ("syamaner/roastpilot-agent", "-bad", "a" * 40, [], False),
        ("syamaner/roastpilot-agent", "feature/x..evil", "a" * 40, [], False),
        ("syamaner/roastpilot-agent", "feature/x", "A" * 40, [], False),
        (
            "syamaner/roastpilot-agent",
            "feature/x",
            "a" * 40,
            [
                "https://github.com/syamaner/roastpilot-agent.git",
                "a" * 40,
                "feature/x",
                "",
                "H tracked.txt\0",
                "a" * 40,
                "a" * 40,
            ],
            True,
        ),
    ],
)
def test_native_codex_git_identity_validates_inputs_and_git_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    branch: str,
    base: str,
    responses: list[str],
    accepts: bool,
) -> None:
    """Git identity fails closed before and during every attestation check."""
    values = iter(responses)
    monkeypatch.setattr(usage_native_codex, "_git", lambda _args: next(values))  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    if accepts:
        assert usage_native_codex._git_identity(repository, branch, base, final=False) == base  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._git_identity(repository, branch, base, final=False)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("moved", [False, True])
def test_native_codex_git_identity_rechecks_head_after_attestation(
    monkeypatch: pytest.MonkeyPatch, final: bool, moved: bool
) -> None:
    """Launch and final identity attestations reject a HEAD movement after checks."""
    base = "a" * 40
    head = "b" * 40 if final else base
    values = iter(
        (
            "https://github.com/syamaner/roastpilot-agent.git",
            head,
            "feature/test",
            "",
            "H tracked.txt\0",
            "c" * 40 if moved else head,
            base,
        )
    )
    monkeypatch.setattr(usage_native_codex, "_git", lambda _args: next(values))  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    if moved:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._git_identity(  # pyright: ignore[reportPrivateUsage]
                "syamaner/roastpilot-agent", "feature/test", base, final=final
            )
    else:
        assert (
            usage_native_codex._git_identity(  # pyright: ignore[reportPrivateUsage]
                "syamaner/roastpilot-agent", "feature/test", base, final=final
            )
            == head
        )


def test_native_codex_descendant_fails_closed_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git timeout or execution error cannot become a successful descendant proof."""

    def timeout(*_args: object, **_kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(usage_native_codex.subprocess, "run", timeout)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._descendant("a" * 40, "b" * 40)  # pyright: ignore[reportPrivateUsage]


def test_native_codex_descendant_ignores_replace_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacement objects cannot make an unrelated final head appear descended."""
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "capture@example.test")
    git("config", "user.name", "Capture")
    (repository / "base.txt").write_text("base\n")
    git("add", "base.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repository / "descendant.txt").write_text("descendant\n")
    git("add", "descendant.txt")
    git("commit", "-m", "descendant")
    descendant = git("rev-parse", "HEAD")
    git("checkout", "--orphan", "unrelated")
    git("rm", "-rf", ".")
    (repository / "unrelated.txt").write_text("unrelated\n")
    git("add", "unrelated.txt")
    git("commit", "-m", "unrelated")
    unrelated = git("rev-parse", "HEAD")
    git("replace", unrelated, descendant)
    monkeypatch.chdir(repository)

    assert usage_native_codex._descendant(base, descendant) is True  # pyright: ignore[reportPrivateUsage]
    assert usage_native_codex._descendant(descendant, unrelated) is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/syamaner/roastpilot-agent.git",
        "git@github.com:syamaner/roastpilot-agent.git",
    ],
)
def test_native_codex_attested_origin_preserves_exact_accepted_form(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    """HTTPS and SSH origin spellings bind provenance exactly as attested."""
    monkeypatch.setattr(usage_native_codex, "_git", lambda _args: origin)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    assert usage_native_codex._attested_origin() == origin  # pyright: ignore[reportPrivateUsage]


def test_native_codex_attested_origin_rejects_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unaccepted origin cannot become rollout provenance."""
    monkeypatch.setattr(usage_native_codex, "_git", lambda _args: "https://example.test/other.git")  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._attested_origin()  # pyright: ignore[reportPrivateUsage]


def test_native_codex_git_attestation_ignores_hostile_repository_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_DIR/GIT_WORK_TREE cannot redirect native attestation away from its worktree."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    for repository in (first, second):
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-b", "feature/test"], cwd=repository, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "capture@example.test"], cwd=repository, check=True
        )
        subprocess.run(["git", "config", "user.name", "Capture"], cwd=repository, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/syamaner/roastpilot-agent.git"],
            cwd=repository,
            check=True,
        )
        (repository / "tracked.txt").write_text(repository.name)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True
        )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=first, check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.chdir(first)
    monkeypatch.setenv("GIT_DIR", str(second / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(second))
    assert (
        usage_native_codex._git_identity(  # pyright: ignore[reportPrivateUsage]
            "syamaner/roastpilot-agent", "feature/test", base, final=False
        )
        == base
    )  # pyright: ignore[reportPrivateUsage]


def test_native_codex_git_attestation_binds_held_worktree_over_core_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository-local core.worktree cannot redirect held native Git queries."""
    repository = tmp_path / "repository"
    redirected = tmp_path / "redirected"
    repository.mkdir()
    redirected.mkdir()
    for command in (
        ["git", "init", "-b", "feature/test"],
        ["git", "config", "user.email", "capture@example.test"],
        ["git", "config", "user.name", "Capture"],
        ["git", "remote", "add", "origin", "https://github.com/syamaner/roastpilot-agent.git"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("held worktree\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected)],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    root = usage_native_codex._open_root(str(repository), private=False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.chdir(redirected)
    try:
        assert (
            usage_native_codex._git_identity(  # pyright: ignore[reportPrivateUsage]
                "syamaner/roastpilot-agent",
                "feature/test",
                base,
                final=False,
                worktree=root,
            )
            == base
        )
    finally:
        os.close(root.descriptor)


def test_native_codex_git_commands_disable_ambient_fsmonitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every native supervisor Git invocation disables repository fsmonitor hooks."""
    commands: list[list[str]] = []
    real_popen = subprocess.Popen

    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        commands.append(command)
        return cast(
            "subprocess.Popen[bytes]", real_popen([sys.executable, "-c", "print('ok')"], **kwargs)
        )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(usage_native_codex.subprocess, "Popen", popen)
    monkeypatch.setattr(usage_native_codex.subprocess, "run", run)
    assert usage_native_codex._git(["status", "--porcelain"]) == "ok"  # pyright: ignore[reportPrivateUsage]
    assert usage_native_codex._descendant("a" * 40, "b" * 40) is True  # pyright: ignore[reportPrivateUsage]
    assert all(command[:3] == ["git", "-c", "core.fsmonitor=false"] for command in commands)
    assert usage_native_codex._GIT_ENV["GIT_NO_REPLACE_OBJECTS"] == "1"  # pyright: ignore[reportPrivateUsage]


def test_native_codex_git_streams_bounded_success_and_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live Git pipe draining returns small output and never exposes failed diagnostics."""
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    scripts = iter(
        (
            "import os; os.write(1, b'clean\\n')",
            "import os; os.write(2, b'SECRET_GIT_DIAGNOSTIC'); raise SystemExit(1)",
        )
    )

    def popen(_command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast(
            "subprocess.Popen[bytes]", real_popen([sys.executable, "-c", next(scripts)], **kwargs)
        )
        processes.append(process)
        return process

    monkeypatch.setattr(usage_native_codex.subprocess, "Popen", popen)
    assert usage_native_codex._git(["status", "--porcelain"]) == "clean"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._git(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]
    assert all(process.poll() is not None for process in processes)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


_REAL_POPEN = subprocess.Popen
_REAL_NATIVE_FAIL = usage_native_codex._fail  # pyright: ignore[reportPrivateUsage]
_REAL_NATIVE_TIME = usage_native_codex.time
_REAL_MONOTONIC = time.monotonic
_REAL_SLEEP = time.sleep
_LIVE_SIGNAL_RETURNCODES = frozenset({-int(signal.SIGTERM), -int(signal.SIGKILL)})


@dataclass(frozen=True)
class _ObservedTime:
    """Module-binding time proxy: real readings recorded and returned unchanged."""

    monotonic: Callable[[], float]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class _NativeGitObservation:
    """Everything one real ``_git`` call exposed to the live-overflow witness."""

    result: str | None
    error: usage_native_codex.NativeCodexCaptureError | None
    readings: tuple[float, ...]
    fail_times: tuple[float, ...]
    max_output_bytes: int
    deadline_seconds: float
    process: subprocess.Popen[bytes]


def _native_git_bindings_are_original() -> bool:
    return (
        usage_native_codex.subprocess.Popen is _REAL_POPEN
        and usage_native_codex._fail is _REAL_NATIVE_FAIL  # pyright: ignore[reportPrivateUsage]
        and usage_native_codex.time is _REAL_NATIVE_TIME
    )


def _native_git_writer(descriptor: int, size: int, *, linger: bool) -> str:
    """Child source: a 20 s SIGALRM self-deadline armed before any write, then a full
    write of ``size`` bytes, then a linger (still under that deadline) or exit 0.

    A self-deadline death is ``-SIGALRM``, never an accepted live-termination code.
    """
    tail = "\ntime.sleep(20)" if linger else ""
    return (
        "import os, signal, time\nsignal.alarm(20)\n"
        f"v = memoryview(b'x' * {size})\n"
        f"while v:\n    v = v[os.write({descriptor}, v):]{tail}"
    )


def _observe_native_git(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    processes: list[subprocess.Popen[bytes]],
    *,
    timeout_seconds: float | None = None,
) -> _NativeGitObservation:
    """Run real ``_git`` once against a real child, observing without altering it."""
    if not _native_git_bindings_are_original():
        pytest.fail("observation binding leaked")
    readings: list[float] = []
    fail_times: list[float] = []

    def popen(_command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast(
            "subprocess.Popen[bytes]", _REAL_POPEN([sys.executable, "-c", script], **kwargs)
        )
        processes.append(process)
        return process

    def recording_fail() -> None:
        fail_times.append(_REAL_MONOTONIC())
        _REAL_NATIVE_FAIL()

    def monotonic() -> float:
        reading = _REAL_MONOTONIC()
        readings.append(reading)
        return reading

    spawned = len(processes)
    with monkeypatch.context() as patch:
        if timeout_seconds is not None:
            patch.setattr(usage_native_codex, "GIT_TIMEOUT_SECONDS", timeout_seconds)
        patch.setattr(usage_native_codex.subprocess, "Popen", popen)
        patch.setattr(usage_native_codex, "_fail", recording_fail)
        patch.setattr(usage_native_codex, "time", _ObservedTime(monotonic, _REAL_SLEEP))
        max_output_bytes = usage_native_codex.MAX_GIT_OUTPUT_BYTES
        deadline_seconds = float(usage_native_codex.GIT_TIMEOUT_SECONDS)
        result: str | None = None
        error: usage_native_codex.NativeCodexCaptureError | None = None
        try:
            result = usage_native_codex._git(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]
        except usage_native_codex.NativeCodexCaptureError as caught:
            if type(caught) is not usage_native_codex.NativeCodexCaptureError:
                raise
            error = caught
        assert len(processes) == spawned + 1
        observation = _NativeGitObservation(
            result,
            error,
            tuple(readings),
            tuple(fail_times),
            max_output_bytes,
            deadline_seconds,
            processes[-1],
        )
    if not _native_git_bindings_are_original():
        pytest.fail("observation binding leaked")
    return observation


def _live_overflow_violations(
    observation: _NativeGitObservation, *, expected_timeout_seconds: float = 5
) -> list[str]:
    """Closed reasons a call is not a live overflow termination; empty means proven."""
    violations: list[str] = []
    if (
        observation.max_output_bytes != 64 * 1024
        or observation.deadline_seconds != expected_timeout_seconds
    ):
        violations.append("wrong production constants")
    if observation.error is None:
        violations.append("did not raise")
    elif observation.error.__context__ is not None:
        violations.append("mapped exception path")
    if len(observation.fail_times) != 1:
        violations.append("fail count != 1")
    readings, fail_times = observation.readings, observation.fail_times
    if (
        not readings
        or not all(math.isfinite(reading) for reading in readings)
        or any(later < earlier for earlier, later in itertools.pairwise(readings))
        or (fail_times and fail_times[0] < readings[0])
    ):
        violations.append("invalid clock observation")
    if readings and fail_times and not fail_times[0] < readings[0] + observation.deadline_seconds:
        violations.append("deadline window reached")
    if observation.process.returncode not in _LIVE_SIGNAL_RETURNCODES:
        violations.append("not terminated live")
    return violations


def _rescue_owned(processes: list[subprocess.Popen[bytes]]) -> list[str]:
    """Kill and reap still-running captured handles, close every captured open pipe.

    Returns secondary faults; only kill/wait depend on the handle still being alive.
    """
    faults: list[str] = []
    for process in processes:
        if process.poll() is None:
            try:
                process.kill()
            except Exception as error:
                faults.append(f"rescue kill failed: {error!r}")
            try:
                process.wait(timeout=5)
            except Exception as error:
                faults.append(f"rescue wait failed: {error!r}")
            if process.returncode is None:
                faults.append(f"rescue could not reap pid {process.pid}")
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception as error:
                    faults.append(f"rescue close failed: {error!r}")
    return faults


@contextmanager
def _owned_native_git_processes() -> Generator[list[subprocess.Popen[bytes]]]:
    """Yield the captured-handle list; rescue faults never replace a primary failure."""
    processes: list[subprocess.Popen[bytes]] = []
    try:
        yield processes
    except BaseException as primary:
        for fault in _rescue_owned(processes):
            primary.add_note(fault)
        raise
    faults = _rescue_owned(processes)
    if faults:
        pytest.fail("; ".join(faults))


@pytest.mark.parametrize("descriptor", [1, 2])
def test_native_codex_git_terminates_on_live_output_overflow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], descriptor: int
) -> None:
    """Neither Git stream can be materialized beyond the fixed live-output cap."""
    assert usage_native_codex.MAX_GIT_OUTPUT_BYTES == 64 * 1024
    assert usage_native_codex.GIT_TIMEOUT_SECONDS == 5
    cap = usage_native_codex.MAX_GIT_OUTPUT_BYTES
    with _owned_native_git_processes() as processes:
        control = _observe_native_git(
            monkeypatch, _native_git_writer(descriptor, cap, linger=False), processes
        )
        if control.error is not None:
            pytest.fail("at-cap output refused")
        assert control.result == ("x" * cap if descriptor == 1 else "")
        assert control.fail_times == ()
        assert control.process.returncode == 0
        overflow = _observe_native_git(
            monkeypatch, _native_git_writer(descriptor, cap + 1, linger=True), processes
        )
        assert _live_overflow_violations(overflow) == []
        assert len(processes) == 2
        for process in processes:
            assert process.poll() is not None
            assert process.stdout is not None and process.stdout.closed
            assert process.stderr is not None and process.stderr.closed
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""


def test_native_codex_git_overflow_witness_rejects_deadline_expiry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W1: a failure raised at the production deadline never passes as a live overflow."""
    with _owned_native_git_processes() as processes:
        observation = _observe_native_git(
            monkeypatch, "import time; time.sleep(20)", processes, timeout_seconds=0.05
        )
        violations = _live_overflow_violations(observation, expected_timeout_seconds=0.05)
        assert "deadline window reached" in violations
        assert "invalid clock observation" not in violations
        assert violations == ["deadline window reached"]
        assert len(observation.fail_times) == 1
        assert observation.process.returncode in _LIVE_SIGNAL_RETURNCODES
        assert observation.process.stdout is not None and observation.process.stdout.closed
        assert observation.process.stderr is not None and observation.process.stderr.closed
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""


def test_native_git_rescue_closes_pipes_of_reaped_handle() -> None:
    """Rescue closes both open pipes of an already-reaped captured handle without signalling."""
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert process.wait(timeout=60) == 0
        assert process.stdout is not None and not process.stdout.closed
        assert process.stderr is not None and not process.stderr.closed
        assert _rescue_owned([process]) == []
        assert process.stdout.closed, "rescue left a reaped handle's stdout pipe open"
        assert process.stderr.closed, "rescue left a reaped handle's stderr pipe open"
    finally:
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()


def test_native_codex_git_terminates_on_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent Git child is terminated and reaped at the fixed timeout boundary."""
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def popen(_command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast(
            "subprocess.Popen[bytes]",
            real_popen([sys.executable, "-c", "import time; time.sleep(2)"], **kwargs),
        )
        processes.append(process)
        return process

    monkeypatch.setattr(usage_native_codex, "GIT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(usage_native_codex.subprocess, "Popen", popen)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._git(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]
    assert processes[0].poll() is not None
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("returncode", [0, 1])
def test_native_codex_descendant_suppresses_git_trace_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
) -> None:
    """A traced Git descendant check never inherits diagnostic output streams."""
    monkeypatch.setenv("GIT_TRACE_SETUP", "1")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(command, returncode, stdout=b"path", stderr=b"trace")

    monkeypatch.setattr(usage_native_codex.subprocess, "run", run)
    assert usage_native_codex._descendant("a" * 40, "b" * 40) is (returncode == 0)  # pyright: ignore[reportPrivateUsage]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_native_codex_usage_root_reattest_rejects_replacement(tmp_path: Path) -> None:
    """A renamed/replaced usage root cannot receive a native capture record."""
    usage = tmp_path / "usage"
    replacement = tmp_path / "replacement"
    usage.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    os.chmod(usage, 0o700)
    os.chmod(replacement, 0o700)
    held = usage_native_codex._open_root(str(usage), private=True)  # pyright: ignore[reportPrivateUsage]
    try:
        os.rename(usage, tmp_path / "old-usage")
        os.rename(replacement, usage)
        with pytest.raises(
            usage_native_codex.NativeCodexCaptureError, match="native Codex capture is invalid"
        ):
            usage_native_codex._reattest_usage_root(held)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(held.descriptor)


def test_native_codex_provider_root_reattest_accepts_stable_and_rejects_replacement(
    tmp_path: Path,
) -> None:
    """Closing topology proof remains bound to the canonical held provider root."""
    provider = tmp_path / "sessions"
    replacement = tmp_path / "replacement"
    provider.mkdir()
    replacement.mkdir()
    os.chmod(provider, 0o755)
    os.chmod(replacement, 0o755)
    held = usage_native_codex._open_root(str(provider), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._reattest_provider_root(held)  # pyright: ignore[reportPrivateUsage]
        os.rename(provider, tmp_path / "old-sessions")
        os.rename(replacement, provider)
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reattest_provider_root(held)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(held.descriptor)


@pytest.mark.parametrize("mode", [0o700, 0o755])
def test_native_codex_worktree_root_accepts_owned_non_writable_modes(
    tmp_path: Path, mode: int
) -> None:
    """The assigned worktree admits private and standard non-writable directory modes."""
    os.chmod(tmp_path, mode)
    held = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._assert_worktree_root(held)  # pyright: ignore[reportPrivateUsage]
        usage_native_codex._reattest_worktree_root(held)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(held.descriptor)


@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_native_codex_worktree_root_rejects_writable_modes(tmp_path: Path, mode: int) -> None:
    """Group- or world-writable assigned worktrees fail before attribution."""
    os.chmod(tmp_path, mode)
    held = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._assert_worktree_root(held)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(held.descriptor)


def test_native_codex_supervisor_usage_root_replacement_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement immediately before append emits neither a record nor RESULT frame."""
    import capture_usage_cli as native_cli

    usage = tmp_path / "usage"
    replacement = tmp_path / "replacement"
    provider_home = tmp_path / "provider-home"
    worktree = tmp_path / "worktree"
    usage.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    (provider_home / "sessions").mkdir(parents=True)
    worktree.mkdir()
    os.chmod(usage, 0o700)
    os.chmod(replacement, 0o700)
    candidate = tmp_path / "leaf.jsonl"
    candidate.write_bytes(b"{}\n")

    class Frames:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    frames = Frames()
    appended: list[object] = []
    original_reattest = usage_native_codex._reattest_usage_root  # pyright: ignore[reportPrivateUsage]

    def replace_then_reattest(root: usage_native_codex._Root) -> None:  # pyright: ignore[reportPrivateUsage]
        os.rename(usage, tmp_path / "old-usage")
        os.rename(replacement, usage)
        original_reattest(root)

    def registered_role(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _role: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        return (
            NATIVE_CODEX_CONFIG_SHA256,
            NATIVE_CODEX_ROLE_SHA256[NativeCodexRole.ENGINEER_BE],
            "high",
            "engineer-be",
        )

    def git_identity(_repository: str, _branch: str, _base: str, *, final: bool) -> str:
        del final
        return "a" * 40

    inventory_calls = 0
    candidate_generation = usage_native_codex._generation(os.stat_result((0,) * 10))  # pyright: ignore[reportPrivateUsage]

    def inventory(_root: usage_native_codex._Root) -> set[tuple[int, int, int]]:  # pyright: ignore[reportPrivateUsage]
        nonlocal inventory_calls
        inventory_calls += 1
        return set() if inventory_calls == 1 else {candidate_generation}

    def metadata(_fd: int, _binding: dict[str, str]) -> usage_native_codex._CandidateMetadata:  # pyright: ignore[reportPrivateUsage]
        return usage_native_codex._CandidateMetadata(True, None)  # pyright: ignore[reportPrivateUsage]

    def parse(
        _fd: int, _binding: dict[str, str], _expected_version: str
    ) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
        return "leaf", (0, 0, 0, 0, 0, 0), "", True, 0

    def new_rollouts(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _before: dict[str, tuple[int, int]],
    ) -> list[tuple[str, int, os.stat_result]]:
        return [("leaf.jsonl", os.open(candidate, os.O_RDONLY), os.stat(candidate))]

    def append(*_args: object, **_kwargs: object) -> None:
        appended.append(object())

    monkeypatch.chdir(worktree)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setattr(usage_native_codex, "_provider_home", lambda: str(provider_home))
    monkeypatch.setattr(usage_native_codex.sys, "stdout", frames)
    monkeypatch.setattr(usage_native_codex, "_registered_role", registered_role)
    monkeypatch.setattr(usage_native_codex, "_git_identity", git_identity)
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(
        usage_native_codex,
        "_attested_origin",
        lambda: "https://github.com/syamaner/roastpilot-agent.git",
    )
    monkeypatch.setattr(usage_native_codex, "_inventory", inventory)
    monkeypatch.setattr(usage_native_codex, "_candidate_metadata", metadata)
    monkeypatch.setattr(usage_native_codex, "_parse_rollout", parse)
    monkeypatch.setattr(usage_native_codex, "_new_rollouts", new_rollouts)
    monkeypatch.setattr(
        usage_native_codex,
        "_terminal_line",
        lambda: b'{"type":"TERMINAL","binding_id":"binding","task_status":"FAILED"}\n',
    )
    monkeypatch.setattr(usage_native_codex, "uuid4", lambda: "binding")
    monkeypatch.setattr(usage_native_codex, "_reattest_usage_root", replace_then_reattest)
    monkeypatch.setattr(native_cli, "append_record", append)
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="leaf",
        role="engineer-be",
        usage_root=str(usage),
        repository="syamaner/roastpilot-agent",
        branch="feature/test",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(
        usage_native_codex.NativeCodexCaptureError, match="native Codex capture is invalid"
    ):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
    assert appended == []
    assert [json.loads(frame)["type"] for frame in frames.values] == ["READY"]


def test_native_codex_descriptor_reads_reject_symlinks_and_size(tmp_path: Path) -> None:
    """Committed config reads stay beneath the held root and never exceed 64 KiB."""
    root = tmp_path / "root"
    config = root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"x" * (usage_native_codex.MAX_COMMITTED_FILE_BYTES + 1))
    held = usage_native_codex._open_root(str(root), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._read_relative(held, (".codex", "config.toml"))  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(held.descriptor)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._open_root(str(link), private=False)  # pyright: ignore[reportPrivateUsage]
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    (intermediate / "jump").symlink_to(root, target_is_directory=True)
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._open_root(str(intermediate / "jump" / ".codex"), private=False)  # pyright: ignore[reportPrivateUsage]


def test_native_codex_root_overlap_uses_held_directory_identities(tmp_path: Path) -> None:
    """Only equal or nested held roots fail; shared filesystem ancestors are allowed."""
    nested = tmp_path / "nested"
    sibling = tmp_path / "sibling"
    other = tmp_path / "other"
    nested.mkdir()
    sibling.mkdir()
    other.mkdir()
    parent = usage_native_codex._open_root(str(tmp_path), private=False)  # pyright: ignore[reportPrivateUsage]
    child = usage_native_codex._open_root(str(nested), private=False)  # pyright: ignore[reportPrivateUsage]
    sibling_root = usage_native_codex._open_root(str(sibling), private=False)  # pyright: ignore[reportPrivateUsage]
    other_root = usage_native_codex._open_root(str(other), private=False)  # pyright: ignore[reportPrivateUsage]
    try:
        usage_native_codex._reject_root_overlap(child, sibling_root, other_root)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reject_root_overlap(parent, child)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reject_root_overlap(child, parent)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._reject_root_overlap(parent, parent)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(parent.descriptor)
        os.close(child.descriptor)
        os.close(sibling_root.descriptor)
        os.close(other_root.descriptor)


def test_native_codex_record_persists_exact_registration_hashes() -> None:
    """Native Codex records reject non-SHA registration bindings and bad topology proofs."""
    started = datetime.now(UTC)
    payload: dict[str, object] = {
        "captured_at": started,
        "task_id": "task-811",
        "slice_id": "native-codex-capture",
        "parent_task_id": "parent-811",
        "task_name": "native-codex-capture",
        "native_role": NativeCodexRole.REPAIR,
        "effort": "medium",
        "config_sha256": NATIVE_CODEX_CONFIG_SHA256,
        "role_sha256": NATIVE_CODEX_ROLE_SHA256[NativeCodexRole.REPAIR],
        "repository": "syamaner/roastpilot-agent",
        "branch": "feature/811-native-codex-capture",
        "base_sha": "a" * 40,
        "launch_head_sha": "a" * 40,
        "final_head_sha": "b" * 40,
        "parent_thread_id": "thread-811",
        "leaf_session_id": "leaf-811",
        "harness_version": "0.147.0",
        "task_status": NativeCodexTaskStatus.SUCCESS,
        "success": True,
        "started_at": started,
        "completed_at": started,
        "elapsed_ms": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "whole_tree_verified": True,
        "subagent_count": 0,
    }
    record = NativeCodexUsageRecord.model_validate(payload)
    assert record.config_sha256 == NATIVE_CODEX_CONFIG_SHA256
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate({**payload, "role_sha256": "not-a-hash"})
    with pytest.raises(ValidationError):
        NativeCodexUsageRecord.model_validate({**payload, "subagent_count": 1})


@pytest.mark.parametrize(
    ("framed", "extra", "accepts"),
    [
        (b'{"type":"TERMINAL"}\n', b"", True),
        (b'{"type":"TERMINAL"}', None, False),
        (b'{"type":"TERMINAL"}\n', None, False),
        (b'{"type":"TERMINAL"}\n', b'{"type":"TERMINAL"}\n', False),
    ],
)
def test_native_codex_terminal_framing_is_nonblocking_and_single_use(
    monkeypatch: pytest.MonkeyPatch, framed: bytes, extra: bytes | None, accepts: bool
) -> None:
    """The supervisor accepts exactly one newline-framed terminal record followed by EOF."""
    fake_stdin = SimpleNamespace(buffer=BytesIO(framed), fileno=lambda: 811)
    monkeypatch.setattr(usage_native_codex.sys, "stdin", fake_stdin)

    def set_blocking(_fd: int, _value: bool) -> None:
        return None

    monkeypatch.setattr(usage_native_codex.os, "set_blocking", set_blocking)

    reads = iter((framed, extra))

    def read_extra(_fd: int, _size: int) -> bytes:
        value = next(reads)
        if value is None:
            raise BlockingIOError()
        return value

    monkeypatch.setattr(usage_native_codex.os, "read", read_extra)
    if accepts:
        assert usage_native_codex._terminal_line() == framed  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._terminal_line()  # pyright: ignore[reportPrivateUsage]


def test_native_codex_terminal_rejects_two_frames_from_one_unbuffered_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One kernel read containing two terminal frames is never accepted."""
    reader, writer = os.pipe()
    try:
        os.write(writer, b'{"type":"TERMINAL"}\n{"type":"TERMINAL"}\n')
        monkeypatch.setattr(usage_native_codex.sys, "stdin", SimpleNamespace(fileno=lambda: reader))
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._terminal_line()  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(writer)
        os.close(reader)


def test_native_codex_terminal_rejects_open_writer_without_delayed_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open writer without EOF cannot leave delayed terminal data admissible."""
    reader, writer = os.pipe()
    try:
        os.write(writer, b'{"type":"TERMINAL"}\n')
        monkeypatch.setattr(usage_native_codex.sys, "stdin", SimpleNamespace(fileno=lambda: reader))
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex._terminal_line()  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(writer)
        os.close(reader)


@pytest.mark.parametrize("failure_call", [2, 3])
def test_native_codex_supervisor_closes_roots_when_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    """Provider/worktree acquisition failures close every previously held root."""
    held: list[int] = []
    calls = 0

    def open_root(_raw: str, *, private: bool) -> usage_native_codex._Root:  # pyright: ignore[reportPrivateUsage]
        nonlocal calls
        del private
        calls += 1
        if calls == failure_call:
            raise usage_native_codex.NativeCodexCaptureError("native Codex capture is invalid")
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        held.append(descriptor)
        state = os.fstat(descriptor)
        return usage_native_codex._Root(descriptor, state.st_dev, state.st_ino)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(usage_native_codex, "_open_root", open_root)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-811")
    arguments = SimpleNamespace(
        task_id="task-811",
        slice_id="slice-811",
        parent_task_id="parent-811",
        task_name="leaf-811",
        role="engineer-be",
        usage_root=str(tmp_path),
        repository="syamaner/roastpilot-agent",
        branch="feature/x",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
    for descriptor in held:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_native_codex_supervisor_rejects_codex_home_before_root_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CODEX_HOME overrides are rejected before any provider root lookup."""
    monkeypatch.setenv("CODEX_HOME", "override")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-811")

    def unexpected_open(_raw: str, *, private: bool) -> NoReturn:
        del private
        pytest.fail("opened")

    monkeypatch.setattr(usage_native_codex, "_open_root", unexpected_open)
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="leaf",
        role="engineer-be",
        usage_root=str(tmp_path),
        repository="syamaner/roastpilot-agent",
        branch="feature/x",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")


def test_native_codex_provider_home_is_closed_to_default_or_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider state accepts only the canonical user home or the platform home."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    default = os.path.abspath(os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, ".codex"))
    assert usage_native_codex._provider_home() == default  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("CODEX_HOME", default)
    assert usage_native_codex._provider_home() == default  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("CODEX_HOME", "/opt/codex")
    assert usage_native_codex._provider_home() == "/opt/codex"  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex._provider_home()  # pyright: ignore[reportPrivateUsage]


def test_native_codex_provider_home_ignores_home_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambient HOME override cannot redirect provider evidence discovery."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-home"))
    assert usage_native_codex._provider_home() == os.path.abspath(  # pyright: ignore[reportPrivateUsage]
        os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, ".codex")
    )


@pytest.mark.parametrize(
    ("role", "status", "child_parent"),
    [
        (NativeCodexRole.ENGINEER_BE, NativeCodexTaskStatus.SUCCESS, None),
        (NativeCodexRole.ENGINEER_FE, NativeCodexTaskStatus.FAILED, None),
        (NativeCodexRole.REPAIR, NativeCodexTaskStatus.CANCELLED, None),
        (NativeCodexRole.ENGINEER_BE, NativeCodexTaskStatus.FAILED, "leaf-811"),
        (NativeCodexRole.ENGINEER_FE, NativeCodexTaskStatus.CANCELLED, "leaf-811"),
        (NativeCodexRole.REPAIR, NativeCodexTaskStatus.SUCCESS, "leaf-811"),
        (NativeCodexRole.ENGINEER_BE, NativeCodexTaskStatus.FAILED, "other-leaf"),
    ],
)
def test_native_codex_supervisor_records_registered_role_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: NativeCodexRole,
    status: NativeCodexTaskStatus,
    child_parent: str | None,
) -> None:
    """The supervisor records each registered role without retaining provider content."""
    import capture_usage_cli as native_cli

    roots: list[usage_native_codex._Root] = []  # pyright: ignore[reportPrivateUsage]
    for name in ("usage", "provider", "worktree"):
        directory = tmp_path / name
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        state = os.fstat(descriptor)
        roots.append(usage_native_codex._Root(descriptor, state.st_dev, state.st_ino))  # pyright: ignore[reportPrivateUsage]

    def open_root(_raw: str, *, private: bool) -> usage_native_codex._Root:  # pyright: ignore[reportPrivateUsage]
        del private
        return roots.pop(0)

    def assert_root(_root: usage_native_codex._Root) -> None:  # pyright: ignore[reportPrivateUsage]
        return None

    def reject_overlap(*_roots: usage_native_codex._Root) -> None:  # pyright: ignore[reportPrivateUsage]
        return None

    inventory_calls = 0
    candidate_generation = usage_native_codex._generation(os.stat_result((0,) * 10))  # pyright: ignore[reportPrivateUsage]

    def inventory(_root: usage_native_codex._Root) -> set[tuple[int, int, int]]:  # pyright: ignore[reportPrivateUsage]
        nonlocal inventory_calls
        inventory_calls += 1
        return set() if inventory_calls == 1 else {candidate_generation}

    def new_rollouts(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _before: set[tuple[int, int, int]],
    ) -> list[tuple[str, int, os.stat_result]]:
        entries = [("leaf.jsonl", 99, os.stat_result((0,) * 10))]
        if child_parent is not None:
            entries.append(("child.jsonl", 98, os.stat_result((0,) * 10)))
        return entries

    def parse_rollout(
        _fd: int, _binding: dict[str, str], _expected_version: str
    ) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
        nonlocal parsed_after_terminal
        if not parsed_after_terminal:
            parsed_after_terminal = True
            time.monotonic()
        if _fd == 98:
            return "child-811", (0, 0, 0, 0, 0, 0), cast(str, child_parent), False, 0
        return "leaf-811", (2, 2, 3, 5, 5, 7), "", True, 0

    def registered_role(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        member: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        time.monotonic()
        return (
            NATIVE_CODEX_CONFIG_SHA256,
            NATIVE_CODEX_ROLE_SHA256[member],
            "medium" if member is NativeCodexRole.REPAIR else "high",
            member.value,
        )

    def git_identity(_repository: str, _branch: str, _base: str, *, final: bool) -> str:
        return "b" * 40 if final and status is NativeCodexTaskStatus.SUCCESS else "a" * 40

    def append(_output: object, record: NativeCodexUsageRecord, **_kwargs: object) -> None:
        captured.append(record)

    monkeypatch.setattr(usage_native_codex, "_open_root", open_root)
    monkeypatch.setattr(usage_native_codex, "_assert_root", assert_root)
    monkeypatch.setattr(usage_native_codex, "_reattest_usage_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reattest_worktree_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reattest_provider_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reject_root_overlap", reject_overlap)
    monkeypatch.setattr(usage_native_codex, "_inventory", inventory)
    monkeypatch.setattr(
        usage_native_codex,
        "_new_rollouts",
        new_rollouts,
    )
    monkeypatch.setattr(
        usage_native_codex,
        "_parse_rollout",
        parse_rollout,
    )
    monkeypatch.setattr(usage_native_codex, "_assert_selected_rollout", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]

    def candidate_metadata(
        descriptor: int, _binding: dict[str, str]
    ) -> usage_native_codex._CandidateMetadata:  # pyright: ignore[reportPrivateUsage]
        return usage_native_codex._CandidateMetadata(  # pyright: ignore[reportPrivateUsage]
            descriptor == 99, "leaf-811" if descriptor == 98 else None
        )

    monkeypatch.setattr(
        usage_native_codex,
        "_candidate_metadata",
        candidate_metadata,
    )
    monkeypatch.setattr(usage_native_codex, "_close", lambda _fd: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_registered_role",
        registered_role,
    )
    monkeypatch.setattr(
        usage_native_codex,
        "_git_identity",
        git_identity,
    )
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(
        usage_native_codex,
        "_attested_origin",
        lambda: "https://github.com/syamaner/roastpilot-agent.git",
    )
    monkeypatch.setattr(usage_native_codex, "_descendant", lambda _base, _head: True)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "uuid4", lambda: "binding-811")
    initial_wall = datetime.now(UTC)
    wall = initial_wall
    parsed_after_terminal = False

    class FixedDateTime:
        """Wall time stays valid while elapsed time comes only from monotonic time."""

        @staticmethod
        def now(_timezone: object) -> datetime:
            return wall

    ticks = iter((1.0, 100.0, 100.125, 1000.0))
    monkeypatch.setattr(usage_native_codex, "datetime", FixedDateTime)
    monkeypatch.setattr(usage_native_codex.time, "monotonic", lambda: next(ticks))

    class Frames:
        """Record the supervisor protocol without relying on process-global capture."""

        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    frames = Frames()
    monkeypatch.setattr(usage_native_codex.sys, "stdout", frames)

    def terminal_line() -> bytes:
        nonlocal wall
        wall = initial_wall - timedelta(days=1)
        return (
            json.dumps(
                {"type": "TERMINAL", "binding_id": "binding-811", "task_status": status.value}
            ).encode()
            + b"\n"
        )

    monkeypatch.setattr(usage_native_codex, "_terminal_line", terminal_line)
    captured: list[NativeCodexUsageRecord] = []
    monkeypatch.setattr(native_cli, "append_record", append)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-811")
    arguments = SimpleNamespace(
        task_id="task-811",
        slice_id="slice-811",
        parent_task_id="parent-task-811",
        task_name="native-codex-capture",
        role=role.value,
        usage_root=str(tmp_path / "usage"),
        repository="syamaner/roastpilot-agent",
        branch="feature/811-native-codex-capture",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    if child_parent is not None:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
        assert captured == []
        assert [json.loads(frame)["type"] for frame in frames.values] == ["READY"]
        return
    assert usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0") == 0
    assert len(captured) == 1
    record = captured[0]
    assert record.native_role is role
    assert record.task_status is status
    assert record.success is (status is NativeCodexTaskStatus.SUCCESS)
    assert record.elapsed_ms == 125
    assert record.started_at == initial_wall
    assert record.completed_at == initial_wall + timedelta(milliseconds=125)
    assert record.completed_at - record.started_at == timedelta(milliseconds=125)
    assert record.subagent_count == 0
    assert record.whole_tree_verified is True
    assert record.config_sha256 == NATIVE_CODEX_CONFIG_SHA256
    assert record.role_sha256 == NATIVE_CODEX_ROLE_SHA256[role]
    assert "SECRET" not in record.model_dump_json()


@pytest.mark.parametrize(
    ("role", "status", "instructions", "observed_version", "rollout_version"),
    [
        (NativeCodexRole.ENGINEER_BE, NativeCodexTaskStatus.SUCCESS, "exact", "0.148.0", "0.148.0"),
        (NativeCodexRole.ENGINEER_FE, NativeCodexTaskStatus.FAILED, "exact", "0.147.0", "0.147.0"),
        (NativeCodexRole.REPAIR, NativeCodexTaskStatus.CANCELLED, "exact", "0.147.0", "0.147.0"),
        (NativeCodexRole.ENGINEER_BE, NativeCodexTaskStatus.FAILED, "stale", "0.147.0", "0.147.0"),
        (
            NativeCodexRole.ENGINEER_BE,
            NativeCodexTaskStatus.FAILED,
            "pre_ready_drift",
            "0.147.0",
            "0.147.0",
        ),
        (
            NativeCodexRole.ENGINEER_BE,
            NativeCodexTaskStatus.FAILED,
            "writable_worktree",
            "0.147.0",
            "0.147.0",
        ),
        (
            NativeCodexRole.ENGINEER_BE,
            NativeCodexTaskStatus.FAILED,
            "version-mismatch",
            "0.148.0",
            "0.147.0",
        ),
        (
            NativeCodexRole.ENGINEER_BE,
            NativeCodexTaskStatus.FAILED,
            "live_pin",
            "0.147.0",
            "0.147.0",
        ),
    ],
)
def test_native_codex_supervisor_real_registered_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: NativeCodexRole,
    status: NativeCodexTaskStatus,
    instructions: str,
    observed_version: str,
    rollout_version: str,
) -> None:
    """A real registered-role lifecycle persists only its closed metadata record."""
    repository = tmp_path / "repository"
    codex_home = tmp_path / "codex-home"
    provider = codex_home / "sessions"
    usage = tmp_path / "usage"
    repository.mkdir()
    provider.mkdir(parents=True)
    usage.mkdir(mode=0o700)
    os.chmod(usage, 0o700)
    if instructions == "live_pin":
        shutil.copytree(_LIVE_CODEX, repository / ".codex")
    else:
        _copy_historical_codex(repository)
    (repository / "README.md").write_text("native Codex lifecycle fixture\n")

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-b", "feature/811-native-codex-capture")
    git("config", "user.email", "capture@example.test")
    git("config", "user.name", "Native Codex Capture")
    git("remote", "add", "origin", "https://github.com/syamaner/roastpilot-agent.git")
    git("add", ".")
    git("commit", "-m", "fixture base")
    base = git("rev-parse", "HEAD")
    exact_instructions = tomllib.loads(
        (repository / ".codex" / "agents" / f"{role.value}.toml").read_text()
    )["developer_instructions"]

    def rollout() -> None:
        # A well-formed but differently bound active session is conclusively unrelated.
        (provider / "active-unrelated.jsonl").write_text(
            json.dumps(
                {
                    "ordinal": 0,
                    "timestamp": "x",
                    "type": "session_meta",
                    "payload": {
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "depth": 1,
                                    "parent_thread_id": "other",
                                    "agent_path": "/root/other",
                                    "agent_role": "engineer-fe",
                                }
                            }
                        }
                    },
                }
            )
            + "\n"
        )
        meta: dict[str, object] = {
            "type": "session_meta",
            "payload": {
                "id": "leaf-811",
                "agent_nickname": "worker-811",
                "agent_path": "/root/native-codex-capture",
                "agent_role": role.value,
                "base_instructions": (
                    exact_instructions if instructions == "exact" else "SECRET_PROVIDER_PROMPT"
                ),
                "originator": "codex-tui",
                "cli_version": rollout_version,
                "context_window": 1,
                "cwd": str(repository),
                "history_mode": "discarded",
                "model_provider": "openai",
                "multi_agent_version": "v2",
                "parent_thread_id": "parent-811",
                "session_id": "leaf-811",
                "thread_source": "subagent",
                "timestamp": "discarded",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "parent-811",
                            "depth": 1,
                            "agent_path": "/root/native-codex-capture",
                            "agent_nickname": "worker-811",
                            "agent_role": role.value,
                        }
                    }
                },
                "git": {
                    "commit_hash": base,
                    "branch": "feature/811-native-codex-capture",
                    "repository_url": "https://github.com/syamaner/roastpilot-agent.git",
                },
            },
        }
        context: dict[str, object] = {
            "type": "turn_context",
            "payload": {
                "model": "gpt-5.6-terra",
                "effort": "medium" if role is NativeCodexRole.REPAIR else "high",
                "approval_policy": "discarded",
                "approvals_reviewer": "discarded",
                "collaboration_mode": "discarded",
                "comp_hash": "discarded",
                "current_date": "discarded",
                "cwd": str(repository),
                "file_system_sandbox_policy": "discarded",
                "multi_agent_version": "v2",
                "permission_profile": "discarded",
                "personality": "discarded",
                "realtime_active": False,
                "sandbox_policy": "discarded",
                "summary": "SECRET_PROVIDER_SUMMARY",
                "timezone": "discarded",
                "turn_id": "discarded",
                "workspace_roots": [],
            },
        }
        cast(dict[str, object], context["payload"]).update(
            _native_leaf_boundary(
                repository, "medium" if role is NativeCodexRole.REPAIR else "high"
            )
        )
        total_usage = {
            "input_tokens": 1,
            "cached_input_tokens": 1,
            "cache_write_input_tokens": 3,
            "output_tokens": 4,
            "reasoning_output_tokens": 4,
            "total_tokens": 5,
        }
        events: list[dict[str, object]] = [
            meta,
            context,
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "collaboration_mode_kind": "discarded",
                    "model_context_window": 1,
                    "started_at": "discarded",
                    "turn_id": "discarded",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "content": "SECRET_PROVIDER_RESPONSE",
                    "id": "discarded",
                    "internal_chat_message_metadata_passthrough": {},
                    "role": "assistant",
                },
            },
            {"type": "world_state", "payload": {"full": False, "state": {}}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {key: 0 for key in total_usage},
                        "model_context_window": 1,
                        "total_token_usage": total_usage,
                    },
                    "rate_limits": {},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "completed_at": "discarded",
                    "duration_ms": 1,
                    "last_agent_message": "SECRET_PROVIDER_RESPONSE",
                    "started_at": "discarded",
                    "time_to_first_token_ms": 1,
                    "turn_id": "discarded",
                },
            },
        ]
        (provider / "leaf.jsonl").write_text(
            "".join(
                json.dumps({"ordinal": number, "timestamp": "discarded", **event}) + "\n"
                for number, event in enumerate(events)
            )
        )

    class CapturedStdout:
        """Minimal text stream exposing the READY frame to the terminal hook."""

        def __init__(self) -> None:
            self.frames: list[str] = []

        def write(self, value: str) -> int:
            self.frames.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    captured_stdout = CapturedStdout()

    def terminal() -> bytes:
        rollout()
        if status is not NativeCodexTaskStatus.SUCCESS:
            (repository / f"{status.value.lower()}-result.txt").write_text("descendant\n")
            git("add", f"{status.value.lower()}-result.txt")
            git("commit", "-m", f"fixture {status.value.lower()} result")
        else:
            (repository / "result.txt").write_text("descendant\n")
            git("add", "result.txt")
            git("commit", "-m", "fixture result")
        ready = json.loads(captured_stdout.frames[-1])
        return (
            json.dumps(
                {"type": "TERMINAL", "binding_id": ready["binding_id"], "task_status": status.value}
            ).encode()
            + b"\n"
        )

    monkeypatch.chdir(repository)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-811")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(usage_native_codex, "_provider_home", lambda: str(codex_home))
    monkeypatch.setattr(usage_native_codex.sys, "stdout", captured_stdout)
    monkeypatch.setattr(usage_native_codex, "_terminal_line", terminal)
    inventory_calls = 0
    pre_ready_inventory_calls = 0
    if instructions == "pre_ready_drift":
        original_inventory = usage_native_codex._inventory  # pyright: ignore[reportPrivateUsage]

        def inventory(
            root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        ) -> usage_native_codex._Inventory | set[tuple[int, int, int]]:  # pyright: ignore[reportPrivateUsage]
            nonlocal pre_ready_inventory_calls
            pre_ready_inventory_calls += 1
            result = original_inventory(root)
            if pre_ready_inventory_calls == 1:
                (repository / "pre-ready-drift.txt").write_text("untracked drift\n")
            return result

        monkeypatch.setattr(usage_native_codex, "_inventory", inventory)
    current_inventory = usage_native_codex._inventory  # pyright: ignore[reportPrivateUsage]

    def counting_inventory(
        root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
    ) -> usage_native_codex._Inventory | set[tuple[int, int, int]]:  # pyright: ignore[reportPrivateUsage]
        nonlocal inventory_calls
        inventory_calls += 1
        return current_inventory(root)

    monkeypatch.setattr(usage_native_codex, "_inventory", counting_inventory)
    if instructions == "writable_worktree":
        os.chmod(repository, 0o770)
    arguments = SimpleNamespace(
        task_id="task-811",
        slice_id="slice-811",
        parent_task_id="parent-task-811",
        task_name="native-codex-capture",
        role=role.value,
        usage_root=str(usage),
        repository="syamaner/roastpilot-agent",
        branch="feature/811-native-codex-capture",
        base_sha=base,
        output=Path(".agent-usage/usage.jsonl"),
    )
    if instructions in {
        "stale",
        "pre_ready_drift",
        "writable_worktree",
        "version-mismatch",
        "live_pin",
    }:
        with pytest.raises(usage_native_codex.NativeCodexCaptureError):
            usage_native_codex.supervise_native_codex(arguments, harness_version=observed_version)
        assert not (usage / ".agent-usage" / "usage.jsonl").exists()
        assert [json.loads(frame)["type"] for frame in captured_stdout.frames] == (
            []
            if instructions in {"pre_ready_drift", "writable_worktree", "live_pin"}
            else ["READY"]
        )
        if instructions == "live_pin":
            assert inventory_calls == 0
        assert "SECRET_PROVIDER_PROMPT" not in capsys.readouterr().out
        return
    assert (
        usage_native_codex.supervise_native_codex(arguments, harness_version=observed_version) == 0
    )
    if instructions == "exact":
        assert inventory_calls >= 1
    assert capsys.readouterr().out == ""
    frames = [json.loads(frame) for frame in captured_stdout.frames]
    assert [frame["type"] for frame in frames] == ["READY", "RESULT"]
    assert frames[1]["success"] is (status is NativeCodexTaskStatus.SUCCESS)
    serialized = (usage / ".agent-usage" / "usage.jsonl").read_text()
    record = NativeCodexUsageRecord.model_validate_json(serialized)
    assert record.native_role is role
    assert record.task_status is status
    assert record.success is (status is NativeCodexTaskStatus.SUCCESS)
    assert record.whole_tree_verified is True
    assert record.subagent_count == 0
    assert record.harness_version == observed_version
    assert record.base_sha == record.launch_head_sha == base
    assert record.final_head_sha == git("rev-parse", "HEAD")
    assert (
        record.config_sha256
        == hashlib.sha256((repository / ".codex/config.toml").read_bytes()).hexdigest()
    )
    role_path = repository / ".codex" / "agents" / f"{role.value}.toml"
    assert record.role_sha256 == hashlib.sha256(role_path.read_bytes()).hexdigest()
    assert record.model_dump_json() == serialized.rstrip()
    for forbidden in ("SECRET_PROVIDER", str(provider), "/host/provider/path"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("topology", "order", "expected_children", "expected_whole", "mismatch"),
    [
        ("sibling", "before", 0, True, None),
        ("sibling", "after", 0, True, None),
        ("child", "before", 1, False, None),
        ("child", "after", 1, False, None),
        ("incomplete-child", "before", 1, False, None),
        ("incomplete-child", "after", 1, False, None),
        ("malformed-sibling", "before", 0, False, None),
        ("malformed-sibling", "after", 0, False, None),
        ("malformed-source", "before", 1, False, None),
        ("malformed-source", "after", 1, False, None),
        ("matching-depth-bool", "after", 0, False, None),
        ("matching-depth-float", "after", 0, False, None),
        ("matching-depth-wrong-int", "after", 0, False, None),
        ("malformed-sibling-success", "after", 0, False, None),
        ("race-child", "after", 0, False, None),
        ("race-sibling", "after", 0, False, None),
        ("race-replacement", "after", 0, False, None),
        ("late-closing-child", "after", 0, False, None),
        ("parent-growth", "after", 0, True, None),
        ("sibling-growth", "after", 0, True, None),
        ("child-growth", "after", 0, False, None),
        ("sibling", "after", 0, False, "cwd"),
        ("sibling", "after", 0, False, "context_cwd"),
        ("sibling", "after", 0, False, "turn_id"),
        ("sibling", "after", 0, False, "turn_id_type"),
        ("sibling", "after", 0, False, "sandbox_policy"),
        ("sibling", "after", 0, False, "workspace_roots"),
        ("sibling", "after", 0, False, "file_system_sandbox_policy"),
        ("sibling", "after", 0, False, "permission_profile"),
        ("sibling", "after", 0, False, "session_multi_agent_v1"),
        ("sibling", "after", 0, False, "session_multi_agent_arbitrary"),
        ("sibling", "after", 0, False, "session_multi_agent_non_string"),
        ("sibling", "after", 0, False, "context_multi_agent_v1"),
        ("sibling", "after", 0, False, "context_multi_agent_arbitrary"),
        ("sibling", "after", 0, False, "context_multi_agent_non_string"),
        ("sibling", "after", 0, False, "model_provider_wrong"),
        ("sibling", "after", 0, False, "model_provider_non_string"),
        ("sibling", "after", 0, False, "context_after_complete"),
        ("sibling", "after", 0, False, "worktree_replacement"),
        ("sibling", "after", 0, False, "worktree_mode_drift"),
        ("sibling", "after", 0, False, "provider_replacement"),
        ("sibling", "after", 0, False, "repository_url"),
        ("sibling", "after", 0, False, "branch"),
        ("sibling", "after", 0, False, "commit_hash"),
    ],
)
def test_native_codex_supervisor_real_rollout_topology_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    topology: str,
    order: str,
    expected_children: int,
    expected_whole: bool,
    mismatch: str | None,
) -> None:
    """Real descriptors classify ordered siblings, children, and incomplete children."""
    worktree = tmp_path / "worktree"
    provider = tmp_path / "codex" / "sessions"
    usage = tmp_path / "usage"
    worktree.mkdir()
    replacement = tmp_path / "replacement-worktree"
    replacement.mkdir()
    provider.mkdir(parents=True)
    provider_replacement = tmp_path / "replacement-provider"
    provider_replacement.mkdir()
    usage.mkdir(mode=0o700)
    os.chmod(usage, 0o700)
    exact_instructions = tomllib.loads(
        (_HISTORICAL_CODEX / "agents" / "engineer-be.toml").read_text()
    )["developer_instructions"]

    def write_rollout(
        path: Path, session: str, parent: str, depth: object, *, complete: bool
    ) -> None:
        meta: dict[str, object] = {
            "id": session,
            "agent_nickname": "worker",
            "agent_path": "/root/native-codex-capture",
            "agent_role": "engineer-be",
            "base_instructions": exact_instructions,
            "cli_version": "0.147.0",
            "context_window": 1,
            "cwd": str(worktree),
            "git": {
                "branch": "feature/test",
                "commit_hash": "a" * 40,
                "repository_url": "https://github.com/syamaner/roastpilot-agent.git",
            },
            "history_mode": "opaque",
            "model_provider": "openai",
            "multi_agent_version": "v2",
            "originator": "codex-tui",
            "parent_thread_id": parent,
            "session_id": session,
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent,
                        "depth": depth,
                        "agent_path": "/root/native-codex-capture",
                        "agent_nickname": "worker",
                        "agent_role": "engineer-be",
                    }
                }
            },
            "thread_source": "subagent",
            "timestamp": "opaque",
        }
        if session == "leaf-811" and mismatch is not None:
            if mismatch == "cwd":
                meta["cwd"] = "untrusted"
            elif mismatch.startswith("session_multi_agent_"):
                meta["multi_agent_version"] = {
                    "session_multi_agent_v1": "v1",
                    "session_multi_agent_arbitrary": "unexpected",
                    "session_multi_agent_non_string": [],
                }[mismatch]
            elif mismatch.startswith("model_provider_"):
                meta["model_provider"] = {
                    "model_provider_wrong": "other",
                    "model_provider_non_string": [],
                }[mismatch]
            elif mismatch not in {
                "context_cwd",
                "turn_id",
                "turn_id_type",
                "sandbox_policy",
                "workspace_roots",
                "file_system_sandbox_policy",
                "permission_profile",
                "session_multi_agent_v1",
                "session_multi_agent_arbitrary",
                "session_multi_agent_non_string",
                "context_multi_agent_v1",
                "context_multi_agent_arbitrary",
                "context_multi_agent_non_string",
                "model_provider_wrong",
                "model_provider_non_string",
                "context_after_complete",
                "worktree_replacement",
                "worktree_mode_drift",
                "provider_replacement",
            }:
                git = cast(dict[str, str], meta["git"])
                git[mismatch] = "untrusted"
        events: list[dict[str, object]] = [{"type": "session_meta", "payload": meta}]
        if complete:
            context: dict[str, object] = {
                key: "opaque"
                for key in usage_native_codex._TURN_CONTEXT_KEYS  # pyright: ignore[reportPrivateUsage]
            }
            context.update(
                {
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                    "multi_agent_version": "v2",
                    "cwd": str(worktree),
                    "realtime_active": False,
                    "workspace_roots": [],
                }
            )
            context.update(_native_leaf_boundary(worktree, "high"))
            if session == "leaf-811" and mismatch == "context_cwd":
                context["cwd"] = "untrusted"
            if session == "leaf-811" and mismatch == "sandbox_policy":
                context["sandbox_policy"] = {"type": "dangerously-unrestricted"}
            if session == "leaf-811" and mismatch == "workspace_roots":
                context["workspace_roots"] = [str(worktree), "/external"]
            if session == "leaf-811" and mismatch == "file_system_sandbox_policy":
                context["file_system_sandbox_policy"] = {"kind": "unrestricted"}
            if session == "leaf-811" and mismatch == "permission_profile":
                context["permission_profile"] = {"type": "unmanaged"}
            if (
                session == "leaf-811"
                and mismatch is not None
                and mismatch.startswith("context_multi_agent_")
            ):
                context["multi_agent_version"] = {
                    "context_multi_agent_v1": "v1",
                    "context_multi_agent_arbitrary": "unexpected",
                    "context_multi_agent_non_string": [],
                }[mismatch]
            totals = {
                key: 0
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                )
            }
            events.extend(
                [
                    {"type": "turn_context", "payload": context},
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "collaboration_mode_kind": "opaque",
                            "model_context_window": 1,
                            "started_at": "opaque",
                            "turn_id": (
                                []
                                if session == "leaf-811" and mismatch == "turn_id_type"
                                else "drift"
                                if session == "leaf-811" and mismatch == "turn_id"
                                else "opaque"
                            ),
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": totals,
                                "model_context_window": 1,
                                "total_token_usage": totals,
                            },
                            "rate_limits": {},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "completed_at": "opaque",
                            "duration_ms": 1,
                            "last_agent_message": "opaque",
                            "started_at": "opaque",
                            "time_to_first_token_ms": 1,
                            "turn_id": "opaque",
                        },
                    },
                ]
            )
            if session == "leaf-811" and mismatch == "context_after_complete":
                events.append(events.pop(1))
        path.write_text(
            "".join(
                json.dumps({"ordinal": number, "timestamp": "opaque", **event}) + "\n"
                for number, event in enumerate(events)
            )
        )

    class Frames:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    frames = Frames()

    def terminal() -> bytes:
        write_rollout(provider / "leaf.jsonl", "leaf-811", "parent-811", 1, complete=True)
        if topology == "parent-growth":
            with (provider / "parent.jsonl").open("ab") as stream:
                stream.write(b'{"opaque":"growth"}\n')
        if topology in {"sibling", "sibling-growth"}:
            write_rollout(provider / "other.jsonl", "other-811", "other-parent", 1, complete=True)
        elif topology in {"malformed-sibling", "malformed-sibling-success"}:
            (provider / "other.jsonl").write_text('{"malformed":"candidate"}\n')
        elif topology == "malformed-source":
            (provider / "other.jsonl").write_text(
                json.dumps(
                    {
                        "ordinal": 0,
                        "timestamp": "opaque",
                        "type": "session_meta",
                        "payload": {"parent_thread_id": "leaf-811", "source": []},
                    }
                )
                + "\n"
            )
        elif topology.startswith("matching-depth-"):
            depth: object = {
                "matching-depth-bool": True,
                "matching-depth-float": 1.0,
                "matching-depth-wrong-int": 2,
            }[topology]
            write_rollout(provider / "other.jsonl", "other-811", "parent-811", depth, complete=True)
        elif topology not in {
            "race-child",
            "race-sibling",
            "race-replacement",
            "late-closing-child",
            "parent-growth",
        }:
            write_rollout(
                provider / "other.jsonl", "child-811", "leaf-811", 2, complete=topology == "child"
            )
        if mismatch == "provider_replacement":
            os.rename(provider, tmp_path / "old-provider")
            os.rename(provider_replacement, provider)
        if order == "before":
            os.rename(provider / "leaf.jsonl", provider / "z-leaf.jsonl")
            os.rename(provider / "other.jsonl", provider / "a-other.jsonl")
        ready = json.loads(frames.values[-1])
        return (
            json.dumps(
                {
                    "type": "TERMINAL",
                    "binding_id": ready["binding_id"],
                    "task_status": (
                        "SUCCESS" if topology == "malformed-sibling-success" else "FAILED"
                    ),
                }
            ).encode()
            + b"\n"
        )

    monkeypatch.chdir(worktree)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-811")
    monkeypatch.setattr(usage_native_codex, "_provider_home", lambda: str(tmp_path / "codex"))
    monkeypatch.setattr(usage_native_codex.sys, "stdout", frames)
    monkeypatch.setattr(usage_native_codex, "_terminal_line", terminal)

    def git_identity(
        _repository: str,
        _branch: str,
        _base: str,
        *,
        final: bool,
        worktree: object | None = None,
    ) -> str:
        return "b" * 40 if final and topology == "malformed-sibling-success" else "a" * 40

    monkeypatch.setattr(usage_native_codex, "_git_identity", git_identity)
    monkeypatch.setattr(usage_native_codex, "_descendant", lambda _base, _head: True)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(
        usage_native_codex,
        "_attested_origin",
        lambda: "https://github.com/syamaner/roastpilot-agent.git",
    )
    original_inventory = usage_native_codex._inventory  # pyright: ignore[reportPrivateUsage]
    inventory_calls = 0

    def inventory(
        root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
    ) -> usage_native_codex._Inventory | set[tuple[int, int, int]]:  # pyright: ignore[reportPrivateUsage]
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            if topology == "race-child":
                write_rollout(
                    provider / "late-child.jsonl", "child-811", "leaf-811", 2, complete=True
                )
            elif topology == "race-sibling":
                write_rollout(
                    provider / "late-sibling.jsonl", "other-811", "other-parent", 1, complete=True
                )
            elif topology == "race-replacement":
                os.unlink(provider / "leaf.jsonl")
                write_rollout(
                    provider / "leaf.jsonl", "replacement-811", "parent-811", 1, complete=True
                )
            elif topology == "sibling-growth" or topology == "child-growth":
                with (provider / "other.jsonl").open("ab") as stream:
                    stream.write(b'{"opaque":"growth"}\n')
            elif mismatch == "worktree_replacement":
                os.rename(worktree, tmp_path / "old-worktree")
                os.rename(replacement, worktree)
            elif mismatch == "worktree_mode_drift":
                os.chmod(worktree, 0o770)
        elif inventory_calls == 3 and topology == "late-closing-child":
            write_rollout(provider / "late-child.jsonl", "child-811", "leaf-811", 2, complete=True)
        return original_inventory(root)

    monkeypatch.setattr(usage_native_codex, "_inventory", inventory)

    if topology == "parent-growth":
        write_rollout(provider / "parent.jsonl", "parent-811", "outer-parent", 1, complete=True)

    def registered_role(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _role: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        return (
            NATIVE_CODEX_CONFIG_SHA256,
            NATIVE_CODEX_ROLE_SHA256[NativeCodexRole.ENGINEER_BE],
            "high",
            "engineer-be",
        )

    monkeypatch.setattr(
        usage_native_codex,
        "_registered_role",
        registered_role,
    )
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="native-codex-capture",
        role="engineer-be",
        usage_root=str(usage),
        repository="syamaner/roastpilot-agent",
        branch="feature/test",
        base_sha="a" * 40,
        output=Path(".agent-usage/usage.jsonl"),
    )
    if mismatch is not None or topology in {
        "race-child",
        "race-sibling",
        "race-replacement",
        "late-closing-child",
        "child",
        "child-growth",
        "matching-depth-bool",
        "matching-depth-float",
        "matching-depth-wrong-int",
        "incomplete-child",
        "malformed-source",
    }:
        with pytest.raises(
            usage_native_codex.NativeCodexCaptureError, match="native Codex capture is invalid"
        ):
            usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
        assert not (usage / ".agent-usage" / "usage.jsonl").exists()
        assert [json.loads(frame)["type"] for frame in frames.values] == ["READY"]
        return
    assert usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0") == 0
    record = NativeCodexUsageRecord.model_validate_json(
        (usage / ".agent-usage" / "usage.jsonl").read_text()
    )
    assert record.subagent_count == expected_children
    assert record.whole_tree_verified is expected_whole
    if topology == "malformed-sibling-success":
        assert record.success is True


@pytest.mark.parametrize("match_count", [0, 2])
def test_native_codex_supervisor_requires_exactly_one_matching_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, match_count: int
) -> None:
    """A contradictory duplicate first-row binding never appends a usage record."""
    import capture_usage_cli as native_cli

    roots: list[usage_native_codex._Root] = []  # pyright: ignore[reportPrivateUsage]
    for name in ("usage", "provider", "worktree"):
        directory = tmp_path / name
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        state = os.fstat(descriptor)
        roots.append(usage_native_codex._Root(descriptor, state.st_dev, state.st_ino))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(usage_native_codex, "_open_root", lambda _raw, private: roots.pop(0))  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reject_root_overlap", lambda *_roots: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_git_identity", lambda *_args, final: "a" * 40)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)

    def registered_role(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _role: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        return (
            NATIVE_CODEX_CONFIG_SHA256,
            NATIVE_CODEX_ROLE_SHA256[NativeCodexRole.ENGINEER_BE],
            "high",
            "engineer-be",
        )

    monkeypatch.setattr(usage_native_codex, "_registered_role", registered_role)
    monkeypatch.setattr(usage_native_codex, "_inventory", lambda _root: set())  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_new_rollouts",
        cast(
            Any,
            lambda _root, _before: [  # pyright: ignore[reportUnknownLambdaType]
                ("a.jsonl", 90 + index, os.stat_result((0,) * 10))
                for index in range(max(1, match_count))
            ],
        ),
    )  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]

    def parse_rollout(
        descriptor: int, _binding: dict[str, str], _expected_version: str
    ) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
        return "leaf", (0, 0, 0, 0, 0, 0), "", descriptor == 90, 0

    monkeypatch.setattr(usage_native_codex, "_parse_rollout", parse_rollout)
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(usage_native_codex, "_close", lambda _fd: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_terminal_line",
        lambda: b'{"type":"TERMINAL","binding_id":"binding","task_status":"FAILED"}\n',
    )
    monkeypatch.setattr(usage_native_codex, "uuid4", lambda: "binding")
    appended: list[object] = []
    monkeypatch.setattr(
        native_cli,
        "append_record",
        cast(Any, lambda *_args, **_kwargs: appended.append(object())),  # pyright: ignore[reportUnknownLambdaType]
    )  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="leaf",
        role="engineer-be",
        usage_root=str(tmp_path / "usage"),
        repository="syamaner/roastpilot-agent",
        branch="feature/x",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
    assert not appended


def test_native_codex_supervisor_rejects_aggregate_actual_candidate_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All parsed candidate byte counts contribute to the immutable aggregate cap."""
    import capture_usage_cli as native_cli

    roots: list[usage_native_codex._Root] = []  # pyright: ignore[reportPrivateUsage]
    for name in ("usage", "provider", "worktree"):
        path = tmp_path / name
        path.mkdir()
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        state = os.fstat(descriptor)
        roots.append(usage_native_codex._Root(descriptor, state.st_dev, state.st_ino))  # pyright: ignore[reportPrivateUsage]
    candidates = [811_101, 811_102]

    def registered(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        role: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        return NATIVE_CODEX_CONFIG_SHA256, NATIVE_CODEX_ROLE_SHA256[role], "high", role.value

    def new_rollouts(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _before: dict[str, tuple[int, int]],
    ) -> list[tuple[str, int, os.stat_result]]:
        return [
            ("one", candidates[0], os.stat_result((0,) * 10)),
            ("two", candidates[1], os.stat_result((0,) * 10)),
        ]

    def parse(
        _fd: int, _binding: dict[str, str], _expected_version: str
    ) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
        return "leaf", (0, 0, 0, 0, 0, 0), "", _fd == candidates[0], 4

    def fail_append(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("append")

    monkeypatch.setattr(usage_native_codex, "MAX_PROVIDER_TOTAL_BYTES", 7)
    monkeypatch.setattr(usage_native_codex, "_open_root", lambda _raw, private: roots.pop(0))  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reject_root_overlap", lambda *_roots: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_git_identity", lambda *_args, final: "a" * 40)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(
        usage_native_codex,
        "_attested_origin",
        lambda: "https://github.com/syamaner/roastpilot-agent.git",
    )
    monkeypatch.setattr(
        usage_native_codex,
        "_registered_role",
        registered,
    )  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_inventory", lambda _root: set())  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_new_rollouts",
        new_rollouts,
    )  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_candidate_metadata",
        lambda _fd, _binding: usage_native_codex._CandidateMetadata(True, None),  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType,reportPrivateUsage]
    )
    monkeypatch.setattr(
        usage_native_codex,
        "_parse_rollout",
        parse,
    )  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        usage_native_codex,
        "_terminal_line",
        lambda: b'{"type":"TERMINAL","binding_id":"binding","task_status":"FAILED"}\n',
    )
    monkeypatch.setattr(usage_native_codex, "uuid4", lambda: "binding")
    monkeypatch.setattr(native_cli, "append_record", fail_append)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="leaf",
        role="engineer-be",
        usage_root=str(tmp_path / "usage"),
        repository="syamaner/roastpilot-agent",
        branch="feature/x",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")


@pytest.mark.parametrize("failure", ["duplicate", "later_parse_error"])
def test_native_codex_supervisor_closes_candidate_fds_on_failed_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Every classified candidate descriptor closes once when selection cannot complete."""
    import capture_usage_cli as native_cli

    roots: list[usage_native_codex._Root] = []  # pyright: ignore[reportPrivateUsage]
    for name in ("usage", "provider", "worktree"):
        directory = tmp_path / name
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        state = os.fstat(descriptor)
        roots.append(usage_native_codex._Root(descriptor, state.st_dev, state.st_ino))  # pyright: ignore[reportPrivateUsage]
    candidates = [811_001, 811_002]

    def open_root(_raw: str, *, private: bool) -> usage_native_codex._Root:  # pyright: ignore[reportPrivateUsage]
        del private
        return roots.pop(0)

    def new_rollouts(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        _before: dict[str, tuple[int, int]],
    ) -> list[tuple[str, int, os.stat_result]]:
        return [
            ("first.jsonl", candidates[0], os.stat_result((0,) * 10)),
            ("second.jsonl", candidates[1], os.stat_result((0,) * 10)),
        ]

    def parse_rollout(
        descriptor: int, _binding: dict[str, str], _expected_version: str
    ) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
        if failure == "later_parse_error" and descriptor == candidates[1]:
            raise usage_native_codex.NativeCodexCaptureError("invalid rollout")
        return "leaf", (0, 0, 0, 0, 0, 0), "", True, 0

    def registered_role(
        _root: usage_native_codex._Root,  # pyright: ignore[reportPrivateUsage]
        member: NativeCodexRole,
    ) -> tuple[str, str, str, str]:
        return (
            NATIVE_CODEX_CONFIG_SHA256,
            NATIVE_CODEX_ROLE_SHA256[member],
            "high",
            member.value,
        )

    closed: list[int] = []

    def close(descriptor: int) -> None:
        """Record supervisor-owned candidate cleanup without closing shared fds."""
        closed.append(descriptor)

    appended: list[NativeCodexUsageRecord] = []

    def append(_output: object, record: NativeCodexUsageRecord, **_kwargs: object) -> None:
        appended.append(record)

    monkeypatch.setattr(usage_native_codex, "_open_root", open_root)
    monkeypatch.setattr(usage_native_codex, "_assert_root", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_reject_root_overlap", lambda *_roots: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_inventory", lambda _root: set())  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_new_rollouts", new_rollouts)
    monkeypatch.setattr(
        usage_native_codex,
        "_candidate_metadata",
        lambda _fd, _binding: usage_native_codex._CandidateMetadata(True, None),  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType,reportPrivateUsage]
    )
    monkeypatch.setattr(usage_native_codex, "_parse_rollout", parse_rollout)
    monkeypatch.setattr(usage_native_codex, "_registered_role", registered_role)
    monkeypatch.setattr(usage_native_codex, "_git_identity", lambda *_args, final: "a" * 40)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(usage_native_codex, "_assert_checkout", lambda _root: None)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    _stub_native_codex_git_administration(monkeypatch)
    monkeypatch.setattr(
        usage_native_codex,
        "_attested_origin",
        lambda: "https://github.com/syamaner/roastpilot-agent.git",
    )
    monkeypatch.setattr(
        usage_native_codex,
        "_terminal_line",
        lambda: b'{"type":"TERMINAL","binding_id":"binding","task_status":"FAILED"}\n',
    )
    monkeypatch.setattr(usage_native_codex, "uuid4", lambda: "binding")
    monkeypatch.setattr(usage_native_codex, "_close", close)
    monkeypatch.setattr(native_cli, "append_record", append)
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    arguments = SimpleNamespace(
        task_id="task",
        slice_id="slice",
        parent_task_id="parent",
        task_name="leaf",
        role="engineer-be",
        usage_root=str(tmp_path / "usage"),
        repository="syamaner/roastpilot-agent",
        branch="feature/x",
        base_sha="a" * 40,
        output="usage.jsonl",
    )
    with pytest.raises(usage_native_codex.NativeCodexCaptureError):
        usage_native_codex.supervise_native_codex(arguments, harness_version="0.147.0")
    assert appended == []
    assert all(closed.count(descriptor) == 1 for descriptor in candidates)


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
        tmp_path,
        session_id,
        NativeClaudeRole.ENGINEER_BE,
        "high",
        expected_version="2.1.233",
        expected_permission_mode="auto",
    )

    assert usage.usage_message_count == 1
    assert usage.input_tokens == 2
    assert usage.cached_input_tokens == 0
    assert usage.cache_creation_input_tokens == 36_369
    assert usage.output_tokens == 9
    assert transcript.read_bytes() == duplicated


def test_owned_transcript_rejects_later_assistant_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every assistant row, not only the first, must match the probed version."""
    session_id = "11111111-1111-4111-8111-111111111233"
    rows = _story_planner_rows(session_id, fixture="qa.jsonl")
    assistant_indices = [index for index, row in enumerate(rows) if row["type"] == "assistant"]
    assert len(assistant_indices) == 2
    assert rows[assistant_indices[0]]["version"] == "2.1.233"
    rows[assistant_indices[1]]["version"] = "2.1.999"
    _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)

    with pytest.raises(
        usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
    ):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.QA,
            "high",
            expected_version="2.1.233",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )


def test_owned_transcript_rejects_version_mismatch_on_user_row_without_handback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A version-bearing user row cannot bypass the parent probe binding."""
    session_id = "11111111-1111-4111-8111-111111111233"
    rows = _story_planner_rows(session_id, fixture="qa.jsonl")
    user_row = next(row for row in rows if row["type"] == "user")
    assert user_row["version"] == "2.1.233"
    user_row["version"] = "2.1.999"
    _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(rows), session_id)

    with pytest.raises(
        usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
    ) as error:
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.QA,
            "high",
            expected_version="2.1.233",
            expected_permission_mode="dontAsk",
            require_handback=True,
        )
    assert "2.1.999" not in str(error.value)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


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
        expected_version="2.1.233",
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
        parse_claude_stream(
            BytesIO(content), expected_version="2.1.233", require_launch_authority=True
        )
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
            tmp_path,
            installed_session,
            role,
            "high",
            expected_version="2.1.233",
            expected_permission_mode="auto",
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
        tmp_path,
        session_id,
        NativeClaudeRole.ENGINEER_BE,
        "high",
        expected_version="2.1.233",
        expected_permission_mode="auto",
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
            expected_version="2.1.233",
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
        tmp_path,
        installed_session_id,
        role,
        effort,
        expected_version="2.1.233",
        expected_permission_mode=permission_mode,
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
            expected_version="2.1.233",
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
    content = content.replace(b'"agentSetting":"security-reviewer"', b'"agentSetting":"qa"')
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
            NativeClaudeRole.QA,
            "high",
            expected_version="2.1.233",
            expected_permission_mode="dontAsk",
        )


def test_native_transcript_binds_model_and_effort_to_the_pinned_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript naming a wrong model family or a wrong effort fails closed."""
    planner_content = (FIXTURES / "claude-2.1.233-transcript" / "story-planner.jsonl").read_bytes()
    planner_session_id = json.loads(planner_content.splitlines()[0])["sessionId"]
    planner_pin = usage_cli._native_role_pin(NativeClaudeRole.STORY_PLANNER)  # pyright: ignore[reportPrivateUsage]
    assert planner_pin.model == "claude-opus-5-5"

    fable_content = planner_content.replace(b'"model":"claude-opus-5"', b'"model":"claude-fable-5"')
    case = tmp_path / "planner"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, fable_content, planner_session_id)
    usage = usage_transcript.parse_owned_transcript(
        case,
        session_id,
        NativeClaudeRole.STORY_PLANNER,
        "high",  # the effort recorded in the 2.1.233 fixture, not the live pin
        expected_version="2.1.233",
        expected_permission_mode="dontAsk",
    )
    assert usage.model == "claude-fable-5" != planner_pin.model

    qa_content = (FIXTURES / "claude-2.1.233-transcript" / "qa.jsonl").read_bytes()
    qa_session_id = json.loads(qa_content.splitlines()[0])["sessionId"]
    qa_pin = usage_cli._native_role_pin(NativeClaudeRole.QA)  # pyright: ignore[reportPrivateUsage]
    assert qa_pin.effort == "medium"
    recorded_qa_effort = "high"  # recorded in the 2.1.233 fixture

    changed_effort = qa_content.replace(b'"effort":"high"', b'"effort":"xhigh"')
    case = tmp_path / "qa"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, changed_effort, qa_session_id)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            case,
            session_id,
            NativeClaudeRole.QA,
            recorded_qa_effort,
            expected_version="2.1.233",
            expected_permission_mode="dontAsk",
        )

    case = tmp_path / "qa-matching"
    case.mkdir()
    _, session_id = _install_owned_transcript(case, monkeypatch, qa_content, qa_session_id)
    usage = usage_transcript.parse_owned_transcript(
        case,
        session_id,
        NativeClaudeRole.QA,
        recorded_qa_effort,
        expected_version="2.1.233",
        expected_permission_mode="dontAsk",
    )
    assert usage.model == "claude-sonnet-5" != qa_pin.model


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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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


@pytest.mark.parametrize("value", [None, {}, True, 1, "0.147", "v0.147.0", "0.147.0.1"])
def test_generic_and_native_worker_records_reject_malformed_harness_versions(value: object) -> None:
    """Every persisted non-Codex record requires one complete observed semver."""
    with pytest.raises(ValidationError, match="harness version"):
        TaskUsageRecord.model_validate({**_task_record().model_dump(), "harness_version": value})
    with pytest.raises(ValidationError, match="harness version"):
        NativeWorkerUsageRecord.model_validate(
            {**_native_record_payload(), "harness_version": value}
        )


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
    assert capsys.readouterr().out == "generic=1 native-worker=3 native-codex=1\n"


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
            expected_version="2.1.233",
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
                expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
            expected_permission_mode="auto",
        )
    assert transcript.read_bytes() == content


@pytest.mark.parametrize(
    "role", [role for role in NativeClaudeRole if role is not NativeClaudeRole.STORY_PLANNER]
)
def test_native_argv_is_exact_and_generic_measurement_stays_ephemeral(
    role: NativeClaudeRole,
) -> None:
    """D161 session persistence is native-only and no native stdout grammar survives.

    D168: also asserts the exact ``--allowedTools`` rule tuple, and that it is
    the final argv element, for every validation role (T1, T2, T3, T6).
    D219: every role except ``story-planner`` (pinned separately below) keeps
    this exact list, with no ``--settings`` option.
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
    assert "--settings" not in argv
    generic = usage_cli._launch_argv(HarnessFamily.CLAUDE, "claude", "model", "high")  # pyright: ignore[reportPrivateUsage]
    assert "--no-session-persistence" in generic
    assert "--add-dir" not in generic
    assert "--allowedTools" not in generic
    assert "--settings" not in generic
    generic_without_effort = usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CLAUDE, "claude", "model", None
    )
    assert "--effort" not in generic_without_effort


def test_story_planner_native_argv_is_confined_exact() -> None:
    """D219 G1: ``story-planner`` carries the fixed read-confinement setting.

    The frozen argv is pinned element by element: ``--settings`` and its exact
    compact JSON sit immediately after ``--setting-sources project`` and
    before ``-p``, and the plan root is the only ``--add-dir``.
    """
    session_id = "11111111-1111-4111-8111-111111111233"
    role = NativeClaudeRole.STORY_PLANNER
    pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        role,
        pin.capability,
        pin.effort,
        session_id,
        BoundRoot(BoundRootKind.PLAN, "/plan/root"),
    )
    assert argv == [
        "claude",
        "--agent",
        "story-planner",
        "--setting-sources",
        "project",
        "--settings",
        '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}',
        "-p",
        "--session-id",
        session_id,
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--add-dir",
        "/plan/root",
        "--permission-mode",
        "dontAsk",
        "--effort",
        "medium",
    ]
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
            "claude", role, pin.capability, pin.effort, session_id, None
        )


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
    cache = f"{root}/cache"
    scrub = render_gate_scrub_prefix(root)
    verify = (
        f"{scrub} {python} -m pytest tests/gate/test_gate_environment.py -q "
        f"-o cache_dir={cache}/pytest --basetemp {tmp}/pytest"
    )
    assert render_allowed_tools(NativeClaudeRole.QA, root) == (
        f"Bash({verify})",
        f"Bash({scrub} {python} -m pytest -o cache_dir={cache}/pytest:*)",
        f"Bash({scrub} {python} -m pyright --pythonpath {python})",
        f"Bash({scrub} {python} -m ruff check .)",
        f"Bash({scrub} {python} -m ruff format --check .)",
    )
    assert render_allowed_tools(NativeClaudeRole.MCP_CONTRACT_CHECKER, root) == (
        f"Bash({verify})",
        f"Bash({scrub} {python} -m pip show coffee-roaster-mcp)",
        f"Bash({scrub} {python} -m pytest tests/test_mcp_client.py -q "
        f"-o cache_dir={cache}/pytest --basetemp {tmp}/pytest)",
    )
    assert render_allowed_tools(NativeClaudeRole.SIM_ROAST_RUNNER, root) == (
        f"Bash({verify})",
        f"Bash({scrub} {python} -m pytest tests/test_milestone1.py "
        f"tests/test_milestone1_real_mcp.py -q -o cache_dir={cache}/pytest "
        f"--basetemp {tmp}/pytest)",
    )
    assert render_validation_commands(NativeClaudeRole.QA, root) == (
        verify,
        f"{scrub} {python} -m pytest -o cache_dir={cache}/pytest",
        f"{scrub} {python} -m pyright --pythonpath {python}",
        f"{scrub} {python} -m ruff check .",
        f"{scrub} {python} -m ruff format --check .",
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
    forbidden_fragments = ("(", ")", ";", "&", "|", "$", "`", "'", '"')
    prefix_entries: list[tuple[NativeClaudeRole, usage_models.ValidationCommand]] = []
    for role, commands in VALIDATION_ROLE_COMMANDS.items():
        assert commands[0] == VALIDATION_ENVIRONMENT_VERIFY_COMMAND
        assert commands.count(VALIDATION_ENVIRONMENT_VERIFY_COMMAND) == 1
        for index, command in enumerate(commands):
            assert command.template.startswith("{scrub} {python} -m ")
            module = command.template.removeprefix("{scrub} {python} -m ").split(" ", 1)[0]
            assert module in allowed_modules
            if module == "pip":
                assert command.template == "{scrub} {python} -m pip show coffee-roaster-mcp"
            if command.kind is ValidationCommandKind.PREFIX:
                prefix_entries.append((role, command))
            if index > 0 and module == "pytest":
                assert "-o cache_dir={cache}/pytest" in command.template
            rendered = command.template.format(
                scrub=render_gate_scrub_prefix("/r"),
                python="/r/venv/bin/python",
                cache="/r/cache",
                tmp="/r/tmp",
            )
            assert rendered.count("*") == 0
            for fragment in forbidden_fragments:
                assert fragment not in rendered
    expected_prefix_entry = (NativeClaudeRole.QA, VALIDATION_ROLE_COMMANDS[NativeClaudeRole.QA][1])
    assert prefix_entries == [expected_prefix_entry]
    for role, commands in VALIDATION_ROLE_COMMANDS.items():
        rules = render_allowed_tools(role, "/r")
        # Exactly one rendered rule per table entry — no bare or extra rule.
        assert len(rules) == len(commands)
        for command, rule in zip(commands, rules, strict=True):
            assert rule.startswith("Bash(") and rule.endswith(")")
            inner = rule.removeprefix("Bash(").removesuffix(")")
            assert inner != "*"
            assert inner.startswith(render_gate_scrub_prefix("/r") + " /r/venv/bin/python -m ")
            assert inner.count("*") <= 1
            if command.kind is ValidationCommandKind.PREFIX:
                assert inner.endswith(":*")
                assert inner.count(":*") == 1
            else:
                assert not inner.endswith(":*")
                assert "*" not in inner


def test_gate_environment_is_derived_from_the_closed_launch_map() -> None:
    """T4/T5: the gate map filters scrubbed launch names and preserves containment."""
    root = "/validated/root"
    values = validation_environment_values(root)
    environment = render_gate_environment(root)
    expected_keys = (
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "RUFF_CACHE_DIR",
        "COVERAGE_FILE",
        "COFFEE_ROAST_LOG_DIR",
        "PIP_CACHE_DIR",
        "PATH",
        "HOME",
        "PYTHONUTF8",
        "RP_VALIDATION_GATE",
    )
    assert tuple(key for key, _ in environment) == expected_keys
    assert all(
        value and not any(character.isspace() for character in value) for _, value in environment
    )
    assert sum(value.count(":") for _, value in environment) == 2
    assert dict(environment)["PATH"] == f"{root}/venv/bin:/usr/bin:/bin"
    assert dict(environment)["HOME"] == f"{root}/tmp"
    assert dict(environment)["PYTHONUTF8"] == "1"
    assert dict(environment)["RP_VALIDATION_GATE"] == "1"
    for key, value in environment:
        if key in values:
            assert value == values[key]
        assert not is_scrubbed_environment_name(key)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ROASTPILOT_DB", True),
        ("roastpilot_advisor__model_slug_by_phase", True),
        ("RoAsTpIlOt_X", True),
        ("PYTHONPATH", True),
        ("OPENROUTER_API_KEY", True),
        ("PYTEST_ADDOPTS", True),
        ("TMPDIR", False),
        ("RP_VALIDATION_GATE", False),
        ("PATH", False),
        ("COFFEE_ROAST_LOG_DIR", False),
    ],
)
def test_scrubbed_environment_name_predicate_is_closed(name: str, expected: bool) -> None:
    """T6: the shared predicate handles namespace casing and exact extra names."""
    assert is_scrubbed_environment_name(name) is expected


def _executable_gate_root(tmp_path: Path) -> Path:
    """Create a disposable rendered-command root backed by this test venv."""
    root = tmp_path / "gate-root"
    root.mkdir()
    (root / "cache").mkdir()
    (root / "tmp").mkdir()
    (root / "venv").symlink_to(Path(sys.prefix))
    return root


def _run_gate_command(
    command: str, environment: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one rendered gate command without exposing test poison values in assertions."""
    return subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
        check=False,
    )


def _poisoned_gate_environment() -> dict[str, str]:
    """Return a synthetic ambient environment containing every #773 poison name."""
    environment = dict(os.environ)
    environment.update(worktree_gate_recipe._ALL_POISON_VALUES)  # pyright: ignore[reportPrivateUsage]
    environment["PYTEST_ADDOPTS"] = "--disable-warnings"
    return environment


def test_rendered_gate_scrub_removes_every_poisoned_name_and_preserves_containment(
    tmp_path: Path,
) -> None:
    """T8/T9/T10/T12: ``env -i`` removes standard and residual poison without disclosure."""
    root = _executable_gate_root(tmp_path)
    prefix = render_gate_scrub_prefix(str(root))
    source = "import os; print('\\n'.join(sorted(os.environ)))"
    command = f"{prefix} {shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    result = _run_gate_command(command, _poisoned_gate_environment())
    assert result.returncode == 0, result.stderr
    names = [line for line in result.stdout.splitlines() if line]
    assert not any(is_scrubbed_environment_name(name) for name in names)
    assert dict(render_gate_environment(str(root))).keys() <= set(names)
    worktree_gate_recipe._assert_no_poisoned_values(  # pyright: ignore[reportPrivateUsage]
        result.stdout + result.stderr
    )
    assert "--disable-warnings" not in result.stdout + result.stderr


def test_rendered_gate_scrub_bypasses_a_path_leading_fake_env_shim(tmp_path: Path) -> None:
    """The closed gate always starts with its canonical ``env`` executable."""
    root = _executable_gate_root(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_env = fake_bin / "env"
    fake_env.write_text("#!/bin/sh\nexit 73\n")
    fake_env.chmod(0o755)
    environment = _poisoned_gate_environment()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    source = "raise SystemExit(0)"
    command = (
        f"{render_gate_scrub_prefix(str(root))} {shlex.quote(sys.executable)} -c "
        f"{shlex.quote(source)}"
    )
    result = _run_gate_command(command, environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(fake_env) not in command
    assert command.startswith(f"{shlex.quote(usage_models.SYSTEM_ENV_EXECUTABLE)} -i ")


def test_system_env_executable_resolution_fails_closed_when_candidates_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical ``env`` resolver rejects when neither absolute candidate is usable."""
    candidates: list[str] = []

    def observe_candidate(path: str) -> str:
        candidates.append(path)
        return path

    def unavailable(_path: str) -> bool:
        return False

    monkeypatch.setattr(usage_models.os.path, "realpath", observe_candidate)
    monkeypatch.setattr(usage_models.os.path, "isfile", unavailable)
    with pytest.raises(RuntimeError) as raised:
        usage_models._resolve_system_env_executable()  # pyright: ignore[reportPrivateUsage]
    assert raised.value.args == ("system env utility is unavailable",)
    assert candidates == ["/usr/bin/env", "/bin/env"]


def test_system_env_executable_resolution_preserves_an_attested_env_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BusyBox target is attested while the fixed ``env`` alias remains executable."""
    alias = "/usr/bin/env"
    busybox = "/bin/busybox"
    checks: list[tuple[str, str]] = []

    def resolve_busybox(_path: str) -> str:
        return busybox

    def regular_file(path: str) -> bool:
        checks.append(("isfile", path))
        return path == busybox

    def executable(path: str, mode: int) -> bool:
        checks.append(("access", path))
        return path == busybox and mode == os.X_OK

    monkeypatch.setattr(usage_models.os.path, "realpath", resolve_busybox)
    monkeypatch.setattr(usage_models.os.path, "isfile", regular_file)
    monkeypatch.setattr(usage_models.os, "access", executable)

    resolved = usage_models._resolve_system_env_executable()  # pyright: ignore[reportPrivateUsage]
    assert resolved == alias
    assert checks == [("isfile", busybox), ("access", busybox)]
    monkeypatch.setattr(usage_models, "SYSTEM_ENV_EXECUTABLE", resolved)
    assert render_gate_scrub_prefix("/validated/root").startswith(f"{alias} -i ")


def test_rendered_gate_scrub_reinstates_exact_root_derived_values(tmp_path: Path) -> None:
    """T10: every retained containment assignment reaches the gate child unchanged."""
    root = _executable_gate_root(tmp_path)
    expected = dict(render_gate_environment(str(root)))
    source = (
        "import os, sys; "
        f"expected = {expected!r}; "
        "ok = all(os.environ[key] == value for key, value in expected.items()); "
        "raise SystemExit(0 if ok else 1)"
    )
    command = (
        f"{render_gate_scrub_prefix(str(root))} {shlex.quote(sys.executable)} -c "
        f"{shlex.quote(source)}"
    )
    result = _run_gate_command(command, _poisoned_gate_environment())
    assert result.returncode == 0, result.stdout + result.stderr


def test_rendered_gate_scrub_handles_a_namespace_name_outside_the_recipe_grammar(
    tmp_path: Path,
) -> None:
    """T9: ``env -i`` removes a valid execve name that #773's text parser cannot see."""
    root = _executable_gate_root(tmp_path)
    prefix = render_gate_scrub_prefix(str(root))
    source = "import os; print('\\n'.join(sorted(os.environ)))"
    environment = _poisoned_gate_environment()
    environment["ROASTPILOT_A B"] = "synthetic-space-name"
    result = _run_gate_command(
        f"{prefix} {shlex.quote(sys.executable)} -c {shlex.quote(source)}", environment
    )
    assert result.returncode == 0, result.stderr
    assert "ROASTPILOT_A B" not in result.stdout.splitlines()
    assert "synthetic-space-name" not in result.stdout + result.stderr


def test_rendered_ruff_gate_runs_from_the_closed_environment(tmp_path: Path) -> None:
    """T11: a real committed gate remains usable after the environment reconstruction."""
    root = _executable_gate_root(tmp_path)
    command = render_validation_commands(NativeClaudeRole.QA, str(root))[3]
    result = _run_gate_command(command, _poisoned_gate_environment())
    assert result.returncode == 0, result.stdout + result.stderr
    worktree_gate_recipe._assert_no_poisoned_values(  # pyright: ignore[reportPrivateUsage]
        result.stdout + result.stderr
    )
    assert "--disable-warnings" not in result.stdout + result.stderr


def _verifier_source_imports(relative_path: str, source: str) -> None:
    """Reject changes to the verifier's finite copied-source import closure."""
    expected = {
        "tests/gate/test_gate_environment.py": {"__future__", "os", "capture_usage_models"},
        "tests/gate/conftest.py": {"__future__", "os", "pathlib", "pytest"},
        ".agents/skills/capture-agent-usage/scripts/capture_usage_models.py": {
            "__future__",
            "os",
            "re",
            "shlex",
            "collections",
            "dataclasses",
            "datetime",
            "enum",
            "typing",
            "pydantic",
        },
    }
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise ValueError("Relative verifier imports require a new closure review")
            roots.add(node.module.split(".", 1)[0])
    if roots != expected[relative_path]:
        raise ValueError(f"Verifier import closure changed: {relative_path}")


def _verifier_root_conftest_is_inert(source: str) -> bool:
    """Check a finite root-conftest tripwire, not transitive import side effects."""
    tree = ast.parse(source)
    forbidden = {"pytest_plugins", "environ", "environb", "putenv", "unsetenv", "setenv", "delenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            return False
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            return False
        if isinstance(node, ast.alias) and (
            node.name.split(".")[-1] in forbidden or node.asname in forbidden
        ):
            return False
    functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    hooks = {node.name for node in functions if node.name.startswith("pytest_")}
    autouse = {
        node.name
        for node in functions
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "pytest"
        and decorator.func.attr == "fixture"
        and any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
    }
    return hooks == {"pytest_sessionfinish"} and autouse == {"reset_non_finite_warning_budget"}


def _verifier_checkout(tmp_path: Path) -> Path:
    """Copy exactly the live verifier sources after finite fidelity checks."""
    source_root = Path(__file__).resolve().parents[1]
    if not _verifier_root_conftest_is_inert((source_root / "tests/conftest.py").read_text()):
        raise ValueError("Root conftest changed; verifier isolation requires review")
    checkout = tmp_path / "verifier-checkout"
    checkout.mkdir()
    for relative_path in (
        "pyproject.toml",
        "tests/gate/conftest.py",
        "tests/gate/test_gate_environment.py",
        ".agents/skills/capture-agent-usage/scripts/capture_usage_models.py",
    ):
        live = source_root / relative_path
        if live.is_symlink() or not live.is_file():
            raise ValueError(f"Verifier source must be a regular non-symlink file: {relative_path}")
        source = live.read_bytes()
        if live.suffix == ".py":
            _verifier_source_imports(relative_path, source.decode("utf-8"))
        copied = checkout / relative_path
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(source)
        if not (
            hashlib.sha256(source).digest()
            == hashlib.sha256(live.read_bytes()).digest()
            == hashlib.sha256(copied.read_bytes()).digest()
        ):
            raise ValueError(f"Verifier source changed during copy: {relative_path}")
    return checkout


def test_verifier_checkout_preserves_current_source_bytes(tmp_path: Path) -> None:
    """P1: the checkout contains exactly four live files and no root conftest."""
    checkout = _verifier_checkout(tmp_path)
    source_root = Path(__file__).resolve().parents[1]
    expected = {
        "pyproject.toml",
        "tests/gate/conftest.py",
        "tests/gate/test_gate_environment.py",
        ".agents/skills/capture-agent-usage/scripts/capture_usage_models.py",
    }
    assert {
        path.relative_to(checkout).as_posix() for path in checkout.rglob("*") if path.is_file()
    } == expected
    assert not (checkout / "tests/conftest.py").exists()
    assert not any(path.is_symlink() for path in checkout.rglob("*"))
    for relative_path in expected:
        assert (
            hashlib.sha256((checkout / relative_path).read_bytes()).digest()
            == hashlib.sha256((source_root / relative_path).read_bytes()).digest()
        )


def test_verifier_checkout_preserves_environment_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: copied predicate decisions and reinstated key order equal live models."""
    checkout = _verifier_checkout(tmp_path)
    path = checkout / ".agents/skills/capture-agent-usage/scripts/capture_usage_models.py"
    name = "_isolated_verifier_models"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    copied = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, copied)
    spec.loader.exec_module(copied)
    assert tuple(key for key, _ in copied.render_gate_environment("")) == tuple(
        key for key, _ in render_gate_environment("")
    )
    for name in (
        "ROASTPILOT_DB",
        "roastpilot_advisor__model_slug_by_phase",
        "RoAsTpIlOt_X",
        "PYTHONPATH",
        "OPENROUTER_API_KEY",
        "PYTEST_ADDOPTS",
        "TMPDIR",
        "RP_VALIDATION_GATE",
        "PATH",
        "COFFEE_ROAST_LOG_DIR",
    ):
        assert copied.is_scrubbed_environment_name(name) is is_scrubbed_environment_name(name)


@pytest.mark.parametrize(
    "relative_path",
    (
        "tests/gate/conftest.py",
        "tests/gate/test_gate_environment.py",
        ".agents/skills/capture-agent-usage/scripts/capture_usage_models.py",
    ),
)
def test_verifier_checkout_import_closure_is_closed(relative_path: str) -> None:
    """P3: any extra local import requires an explicit closure review."""
    source = (Path(__file__).resolve().parents[1] / relative_path).read_text()
    _verifier_source_imports(relative_path, source)
    with pytest.raises(ValueError, match="import closure changed"):
        _verifier_source_imports(relative_path, source + "\nimport capture_usage_cli\n")


def test_verifier_target_preserves_five_real_command_contexts() -> None:
    """P4: five direct real calls retain their order and three isolated contexts."""
    tree = ast.parse(
        inspect.getsource(
            test_gate_environment_verifier_fails_closed_for_clean_leaked_missing_and_absent_cases
        )
    )
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert all(isinstance(node, (ast.Expr, ast.Assign, ast.Assert)) for node in function.body)
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    gates = sorted(
        (
            node
            for node in calls
            if isinstance(node.func, ast.Name) and node.func.id == "_run_gate_command"
        ),
        key=lambda node: node.lineno,
    )
    assert len(gates) == 5
    assigned = [
        node.targets[0].id
        for node in function.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.value in gates
    ]
    assert assigned == ["clean", "leaked", "leaked_pytest_addopts", "missing", "absent"]
    setups = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_verifier_checkout"
    ]
    assert len(setups) == 1
    assert ast.dump(setups[0]) == ast.dump(
        ast.parse("_verifier_checkout(tmp_path)", mode="eval").body
    )
    setup = next(
        node for node in function.body if isinstance(node, ast.Assign) and node.value is setups[0]
    )
    assert len(setup.targets) == 1 and isinstance(setup.targets[0], ast.Name)
    for index, gate in enumerate(gates):
        cwd = [keyword.value for keyword in gate.keywords if keyword.arg == "cwd"]
        if index in (1, 2, 3):
            assert len(cwd) == 1 and isinstance(cwd[0], ast.Name)
            assert cwd[0].id == setup.targets[0].id
        else:
            assert cwd == []
    renders = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "render_validation_commands"
    ]
    assert len(renders) == 1
    assert any(
        isinstance(node, ast.Subscript)
        and node.value is renders[0]
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
        for node in ast.walk(function)
    )
    forbidden = {"CompletedProcess", "monkeypatch", "skip", "mark", "subprocess"}
    assert not any(
        (isinstance(node, ast.Name) and node.id in forbidden)
        or (isinstance(node, ast.Attribute) and node.attr in forbidden)
        for node in ast.walk(function)
    )


def test_verifier_root_conftest_tripwire_rejects_environment_mutation() -> None:
    """P5: finite root-conftest assumptions reject a new environment mutation."""
    source = (Path(__file__).resolve().parents[1] / "tests/conftest.py").read_text()
    assert _verifier_root_conftest_is_inert(source)
    injected = source.replace(
        "    reset_non_finite_telemetry_warnings()",
        '    os.environ.pop("ROASTPILOT_DB", None)\n    reset_non_finite_telemetry_warnings()',
        1,
    )
    assert injected != source
    assert not _verifier_root_conftest_is_inert(injected)


def test_gate_environment_verifier_fails_closed_for_clean_leaked_missing_and_absent_cases(
    tmp_path: Path,
) -> None:
    """T13-T16: the first gate verifies names only and cannot silently skip collection."""
    root = _executable_gate_root(tmp_path)
    checkout = _verifier_checkout(tmp_path)
    command = render_validation_commands(NativeClaudeRole.QA, str(root))[0]
    clean = _run_gate_command(command, _poisoned_gate_environment())
    assert clean.returncode == 0, clean.stdout + clean.stderr
    poison_value = "synthetic-verifier-poison"
    scrub = render_gate_scrub_prefix(str(root))
    leaked = _run_gate_command(
        command.replace(scrub, scrub.replace(" -i ", f" -i ROASTPILOT_DB={poison_value} ", 1), 1),
        _poisoned_gate_environment(),
        cwd=checkout,
    )
    assert leaked.returncode != 0
    assert "ROASTPILOT_DB" in leaked.stdout + leaked.stderr
    assert poison_value not in leaked.stdout + leaked.stderr
    pytest_addopts_value = "--disable-warnings"
    leaked_pytest_addopts = _run_gate_command(
        command.replace(
            scrub, scrub.replace(" -i ", f" -i PYTEST_ADDOPTS={pytest_addopts_value} ", 1), 1
        ),
        _poisoned_gate_environment(),
        cwd=checkout,
    )
    assert leaked_pytest_addopts.returncode != 0
    assert "PYTEST_ADDOPTS" in leaked_pytest_addopts.stdout + leaked_pytest_addopts.stderr
    assert pytest_addopts_value not in leaked_pytest_addopts.stdout + leaked_pytest_addopts.stderr
    missing = _run_gate_command(
        command.replace(f" TMPDIR={root}/tmp", "", 1), _poisoned_gate_environment(), cwd=checkout
    )
    assert missing.returncode != 0
    assert "TMPDIR" in missing.stdout + missing.stderr
    bare = command.removeprefix(render_gate_scrub_prefix(str(root)) + " ")
    bare_environment = _poisoned_gate_environment()
    bare_environment.pop("RP_VALIDATION_GATE", None)
    absent = _run_gate_command(bare, bare_environment)
    assert absent.returncode == 5


@pytest.mark.parametrize(
    "gate_target",
    ("tests/gate", str(Path(__file__).resolve().parents[1] / "tests" / "gate")),
    ids=("relative", "absolute"),
)
def test_gate_directory_is_not_collected_without_the_validation_sentinel(gate_target: str) -> None:
    """T17: relative and absolute gate targets fail closed without the sentinel."""
    environment = dict(os.environ)
    environment.pop("RP_VALIDATION_GATE", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", gate_target],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 5


@pytest.mark.parametrize(
    "targets",
    [
        (
            "tests/gate",
            "tests/test_capture_agent_usage.py::test_gate_directory_is_not_collected_without_the_validation_sentinel",
        ),
        (
            "tests/test_capture_agent_usage.py::test_gate_directory_is_not_collected_without_the_validation_sentinel",
            "tests/gate",
        ),
        (
            str(Path(__file__).resolve().parents[1] / "tests" / "gate"),
            "tests/test_capture_agent_usage.py::test_gate_directory_is_not_collected_without_the_validation_sentinel",
        ),
        (
            "tests/test_capture_agent_usage.py::test_gate_directory_is_not_collected_without_the_validation_sentinel",
            str(Path(__file__).resolve().parents[1] / "tests" / "gate"),
        ),
    ],
)
def test_mixed_explicit_gate_targets_fail_closed_without_the_sentinel(
    targets: tuple[str, str],
) -> None:
    """Explicit gate targets fail closed in either mixed-argument ordering."""
    environment = dict(os.environ)
    environment.pop("RP_VALIDATION_GATE", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *targets],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 5
    assert "validation gate sentinel is absent" in result.stdout + result.stderr


def test_gate_target_guard_preserves_broad_discovery_and_gateway_boundary() -> None:
    """Broad discovery remains legal and a gateway prefix is not a gate target."""
    environment = dict(os.environ)
    environment.pop("RP_VALIDATION_GATE", None)
    broad = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert broad.returncode == 0, broad.stdout + broad.stderr
    gateway = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/gateway"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert gateway.returncode != 5
    assert "validation gate sentinel is absent" not in gateway.stdout + gateway.stderr


_WITNESS_NODEID = (
    "tests/test_capture_agent_usage.py::"
    "test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper"
)
_GATE_PREFIX = "tests/gate/"
_PARALLEL_MODE: tuple[str, ...] = ("-n", "4", "--dist", "worksteal")


class _WitnessContractError(AssertionError):
    """A fail-closed nested-witness proof or outcome check did not hold (#1015, D217)."""


class _PytestOutcome(Enum):
    """Closed set of pytest final-line outcome kinds the nested witness understands."""

    PASSED = auto()
    FAILED = auto()
    SKIPPED = auto()
    XFAILED = auto()
    XPASSED = auto()
    ERROR = auto()
    DESELECTED = auto()
    WARNING = auto()


@dataclass(frozen=True)
class _WitnessSelection:
    """One nested-witness pytest selection and its pinned execution mode.

    Attributes:
        name: Stable selection name used to key collected node IDs.
        selection: Pytest argv shared verbatim by collection and execution.
        mode: Execution-only argv suffix (scheduling), never used for collection.
    """

    name: str
    selection: tuple[str, ...]
    mode: tuple[str, ...]


_WITNESS_SELECTIONS: tuple[_WitnessSelection, ...] = (
    _WitnessSelection(
        "ordinary",
        (
            "tests",
            "-m",
            "not serial and not stress",
            "--ignore=tests/test_packaging.py",
            "--deselect",
            _WITNESS_NODEID,
        ),
        _PARALLEL_MODE,
    ),
    _WitnessSelection(
        "serial-stress",
        (
            "tests",
            "-m",
            "serial or stress",
            "--ignore=tests/test_packaging.py",
            "--deselect",
            _WITNESS_NODEID,
        ),
        (),
    ),
    _WitnessSelection("packaging", ("tests/test_packaging.py", "--deselect", _WITNESS_NODEID), ()),
)
_BROAD_SELECTION: tuple[str, ...] = ("tests",)
_GATE_SELECTION: tuple[str, ...] = ("tests/gate",)

# Receives the pytest argv that follows the rendered QA wrapper prefix.
_WitnessRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]

_COLLECTED_NODE = re.compile(r"tests/[^\s:]+\.py::\S(?:.*\S)?")
_COLLECTION_TRAILER = re.compile(
    r"(?:(?P<all>[1-9]\d*) tests? collected"
    r"|(?P<sel>[1-9]\d*)/(?P<tot>[1-9]\d*) tests collected \((?P<des>[1-9]\d*) deselected\))"
    r" in \d+\.\d+s(?: \(\d+:\d{2}:\d{2}\))?"
)
_EXECUTION_TRAILER = re.compile(
    r"(?P<parts>[1-9]\d* [a-z]+(?:, [1-9]\d* [a-z]+)*) in \d+\.\d+s(?: \(\d+:\d{2}:\d{2}\))?"
)
_GATE_NODE = re.compile(
    r"tests/gate/test_[a-z0-9_]+\.py::(?P<function>test_[a-z0-9_]+)(?:\[[^\]\s]+\])?"
)
# The single outcome-token lookup table: outcomes are never string-compared elsewhere.
_OUTCOME_TOKENS: dict[str, _PytestOutcome] = {
    "passed": _PytestOutcome.PASSED,
    "failed": _PytestOutcome.FAILED,
    "skipped": _PytestOutcome.SKIPPED,
    "xfailed": _PytestOutcome.XFAILED,
    "xpassed": _PytestOutcome.XPASSED,
    "error": _PytestOutcome.ERROR,
    "errors": _PytestOutcome.ERROR,
    "deselected": _PytestOutcome.DESELECTED,
    "warning": _PytestOutcome.WARNING,
    "warnings": _PytestOutcome.WARNING,
}
_ACCEPTED_PAYLOAD_OUTCOMES = frozenset(
    {
        _PytestOutcome.PASSED,
        _PytestOutcome.SKIPPED,
        _PytestOutcome.XFAILED,
        _PytestOutcome.XPASSED,
    }
)
_NON_NODE_OUTCOMES = frozenset({_PytestOutcome.DESELECTED, _PytestOutcome.WARNING})
_DIAGNOSTIC_TAIL_LINES = 40


def _child_diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    """Return a bounded stdout/stderr tail for a fail-closed witness message."""
    stdout_tail = "\n".join(result.stdout.splitlines()[-_DIAGNOSTIC_TAIL_LINES:])
    stderr_tail = "\n".join(result.stderr.splitlines()[-_DIAGNOSTIC_TAIL_LINES:])
    return (
        f"exit={result.returncode}\n--- stdout tail ---\n{stdout_tail}"
        f"\n--- stderr tail ---\n{stderr_tail}"
    )


_WITNESS_FAILURE_EVIDENCE_DIR = "witness-failure-evidence"
_WITNESS_FAILURE_STREAM_LIMIT = 16 * 1024 * 1024
_WITNESS_FAILURE_SCHEMA = "roastpilot-witness-child-failure/1"


def _write_failed_child_evidence(
    directory: Path, index: int, result: subprocess.CompletedProcess[str]
) -> None:
    """Retain bounded decoded child streams privately; metadata attests completion."""
    try:
        streams: dict[str, dict[str, object]] = {}
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        for name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
            encoded = stream.encode("utf-8")
            retained = len(encoded) <= _WITNESS_FAILURE_STREAM_LIMIT
            streams[name] = {
                "retained": retained,
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "reason": "ok" if retained else "overflow",
            }
            if retained:
                descriptor = os.open(directory / f"child-{index:02d}.{name}", flags, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(encoded)
        metadata = {
            "schema": _WITNESS_FAILURE_SCHEMA,
            "sequence": index,
            "exit": result.returncode,
            "representation": "utf8-of-decoded-universal-newline-text",
            "complete": all(stream["retained"] for stream in streams.values()),
            "streams": streams,
        }
        encoded_metadata = json.dumps(metadata, sort_keys=True, allow_nan=False).encode("utf-8")
        descriptor = os.open(directory / f"child-{index:02d}.json", flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded_metadata)
    except (OSError, ValueError):
        return


def _retain_failed_child_streams(run: _WitnessRunner, evidence: Path) -> _WitnessRunner:
    """Adapt one caller's runner without changing its calls, results or failures."""
    sequence = itertools.count()
    created = False

    def retained(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        nonlocal created
        index = next(sequence)
        result = run(argv)
        if result.returncode != 0 and not created:
            created = True
            try:
                evidence.mkdir(mode=0o700)
                _write_failed_child_evidence(evidence, index, result)
            except (OSError, ValueError):
                pass
        return result

    return retained


def _final_line(result: subprocess.CompletedProcess[str]) -> str:
    """Return the last non-blank stdout line, failing closed when there is none."""
    lines = [line for line in result.stdout.splitlines() if line.strip() != ""]
    if not lines:
        raise _WitnessContractError(f"child produced no final line\n{_child_diagnostic(result)}")
    return lines[-1]


def _parse_collection(result: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    """Parse a ``--collect-only`` child's leading node block under pyproject's ``-q``.

    Args:
        result: The completed collection child.

    Returns:
        The collected node IDs in pytest's listing order.

    Raises:
        _WitnessContractError: On a nonzero exit, a missing blank separator, an
            empty or non-node block line, a duplicate node, a trailer outside the
            closed grammar, or a trailer count that disagrees with the block.
    """
    if result.returncode != 0:
        raise _WitnessContractError(f"collection exited nonzero\n{_child_diagnostic(result)}")
    lines = result.stdout.splitlines()
    if "" not in lines:
        raise _WitnessContractError(
            f"collection has no blank separator\n{_child_diagnostic(result)}"
        )
    block = tuple(lines[: lines.index("")])
    if not block:
        raise _WitnessContractError(f"collection node block is empty\n{_child_diagnostic(result)}")
    for line in block:
        if _COLLECTED_NODE.fullmatch(line) is None:
            raise _WitnessContractError(f"collection line is not a node ID: {line!r}")
    if len(set(block)) != len(block):
        raise _WitnessContractError("collection node block contains a duplicate node ID")
    trailer = _COLLECTION_TRAILER.fullmatch(_final_line(result))
    if trailer is None:
        raise _WitnessContractError(f"collection trailer is malformed\n{_child_diagnostic(result)}")
    if trailer.group("all") is not None:
        count = int(trailer.group("all"))
    else:
        count = int(trailer.group("sel"))
        if count + int(trailer.group("des")) != int(trailer.group("tot")):
            raise _WitnessContractError("collection trailer selected+deselected != total")
    if len(block) != count:
        raise _WitnessContractError(
            f"collection block has {len(block)} node IDs but the trailer reports {count}"
        )
    return block


def _parse_outcomes(result: subprocess.CompletedProcess[str]) -> dict[_PytestOutcome, int]:
    """Parse an execution child's single final outcome line through the closed token table.

    Args:
        result: The completed execution child.

    Returns:
        The count for each outcome named on the final line.

    Raises:
        _WitnessContractError: On a nonzero exit (even with a clean-looking
            trailer), a missing or malformed trailer, an unknown token, or a
            repeated outcome.
    """
    if result.returncode != 0:
        raise _WitnessContractError(f"execution exited nonzero\n{_child_diagnostic(result)}")
    trailer = _EXECUTION_TRAILER.fullmatch(_final_line(result))
    if trailer is None:
        raise _WitnessContractError(f"execution trailer is malformed\n{_child_diagnostic(result)}")
    outcomes: dict[_PytestOutcome, int] = {}
    for part in trailer.group("parts").split(", "):
        count_text, token = part.split(" ")
        outcome = _OUTCOME_TOKENS.get(token)
        if outcome is None:
            raise _WitnessContractError(f"execution trailer has unknown outcome token {token!r}")
        if outcome in outcomes:
            raise _WitnessContractError(f"execution trailer repeats outcome {outcome.name}")
        outcomes[outcome] = int(count_text)
    return outcomes


def _require_payload_outcome(
    result: subprocess.CompletedProcess[str], expected: int
) -> dict[_PytestOutcome, int]:
    """Require a payload child to account for exactly its collected selection.

    Args:
        result: The completed payload execution child.
        expected: The node count collected for the same selection argv.

    Returns:
        The parsed outcome counts.

    Raises:
        _WitnessContractError: When parsing fails, any failure or error is
            reported, or passed+skipped+xfailed+xpassed differs from ``expected``.
    """
    outcomes = _parse_outcomes(result)
    if _PytestOutcome.FAILED in outcomes or _PytestOutcome.ERROR in outcomes:
        raise _WitnessContractError(
            f"payload reported failures or errors\n{_child_diagnostic(result)}"
        )
    accounted = sum(
        count for outcome, count in outcomes.items() if outcome in _ACCEPTED_PAYLOAD_OUTCOMES
    )
    if accounted != expected:
        raise _WitnessContractError(
            f"payload accounted for {accounted} nodes but {expected} were collected"
        )
    return outcomes


def _require_gate_probe_outcome(result: subprocess.CompletedProcess[str], expected: int) -> None:
    """Require the gate-only probe to report exactly ``expected`` passed nodes and nothing else.

    Counts that include skips, xfails or xpasses never prove a gate pass.

    Args:
        result: The completed gate-probe execution child.
        expected: The non-empty separately collected ``tests/gate`` node count.

    Raises:
        _WitnessContractError: When parsing fails or the node-bearing outcomes
            are not exactly ``{PASSED: expected}``.
    """
    outcomes = _parse_outcomes(result)
    node_outcomes = {
        outcome: count for outcome, count in outcomes.items() if outcome not in _NON_NODE_OUTCOMES
    }
    if expected < 1 or node_outcomes != {_PytestOutcome.PASSED: expected}:
        raise _WitnessContractError(
            f"gate probe outcomes {sorted(o.name for o in node_outcomes)} are not "
            f"exactly {expected} passed\n{_child_diagnostic(result)}"
        )


def _require_self_once(broad: tuple[str, ...]) -> None:
    """Require the witness node to appear exactly once in the broad collection."""
    if broad.count(_WITNESS_NODEID) != 1:
        raise _WitnessContractError("broad collection must contain the witness node exactly once")


def _require_partition(broad: tuple[str, ...], collected: Mapping[str, tuple[str, ...]]) -> None:
    """Require the selections to partition the broad collection minus the witness node.

    Args:
        broad: The wrapped broad collection.
        collected: The wrapped collection of each ``_WITNESS_SELECTIONS`` entry, by name.

    Raises:
        _WitnessContractError: When the witness is not exactly once in ``broad``,
            the selection names differ, any selection is empty, contains a
            duplicate or the witness, two selections overlap, or the union is not
            exactly ``broad`` minus the witness.
    """
    _require_self_once(broad)
    if set(collected) != {selection.name for selection in _WITNESS_SELECTIONS}:
        raise _WitnessContractError(f"selection names {sorted(collected)} are not the pinned set")
    union: set[str] = set()
    for name, nodes in collected.items():
        if not nodes:
            raise _WitnessContractError(f"selection {name!r} is empty")
        if len(set(nodes)) != len(nodes):
            raise _WitnessContractError(f"selection {name!r} contains a duplicate node ID")
        if _WITNESS_NODEID in nodes:
            raise _WitnessContractError(f"selection {name!r} contains the witness node")
        overlap = union & set(nodes)
        if overlap:
            raise _WitnessContractError(
                f"selection {name!r} overlaps another: {sorted(overlap)[:5]}"
            )
        union |= set(nodes)
    expected = set(broad) - {_WITNESS_NODEID}
    if union != expected:
        raise _WitnessContractError(
            f"selections drop {sorted(expected - union)[:5]} and add {sorted(union - expected)[:5]}"
        )


def _gate_keyword(gate_nodeids: tuple[str, ...]) -> str:
    """Return the closed ``-k`` keyword that selects exactly the gate functions.

    Args:
        gate_nodeids: The separately collected non-empty ``tests/gate`` node IDs.

    Returns:
        The sorted, de-duplicated gate function names joined by ``" or "``.

    Raises:
        _WitnessContractError: When the tuple is empty or any node ID falls
            outside the closed module-level gate-function grammar.
    """
    if not gate_nodeids:
        raise _WitnessContractError("gate collection is empty")
    functions: set[str] = set()
    for nodeid in gate_nodeids:
        match = _GATE_NODE.fullmatch(nodeid)
        if match is None:
            raise _WitnessContractError(f"gate node ID {nodeid!r} is outside the closed grammar")
        functions.add(match.group("function"))
    return " or ".join(sorted(functions))


def _require_gate_in_ordinary(gate: tuple[str, ...], ordinary: tuple[str, ...]) -> None:
    """Require a non-empty gate set that the ordinary selection fully contains."""
    if not gate:
        raise _WitnessContractError("gate collection is empty")
    if not all(nodeid.startswith(_GATE_PREFIX) for nodeid in gate):
        raise _WitnessContractError("gate collection contains a node outside tests/gate/")
    if not set(gate) <= set(ordinary):
        raise _WitnessContractError("gate checks are absent from the ordinary selection")


def _require_probe_matches_gate(probe: tuple[str, ...], gate: tuple[str, ...]) -> None:
    """Require the broad gate-only probe to collect exactly the gate set."""
    if set(probe) != set(gate) or len(probe) != len(gate):
        raise _WitnessContractError(
            f"gate probe collects {sorted(set(probe) ^ set(gate))[:5]} beyond the gate set"
        )


def _run_partitioned_witness(run: _WitnessRunner) -> None:
    """Prove the nested selections and gate probe, then execute them in pinned order.

    Every collection proof completes before any execution starts, and the first
    failure raises without running any later child (#1015, D217).

    Args:
        run: Runs one pytest argv under the rendered QA wrapper prefix.

    Raises:
        _WitnessContractError: On the first failed proof or outcome check.
    """
    broad = _parse_collection(run(_BROAD_SELECTION + ("--collect-only",)))
    _require_self_once(broad)
    collected: dict[str, tuple[str, ...]] = {}
    for selection in _WITNESS_SELECTIONS:
        collected[selection.name] = _parse_collection(
            run(selection.selection + ("--collect-only",))
        )
    _require_partition(broad, collected)
    gate = _parse_collection(run(_GATE_SELECTION + ("--collect-only",)))
    _require_gate_in_ordinary(gate, collected["ordinary"])
    probe_selection = ("tests", "-k", _gate_keyword(gate), "--deselect", _WITNESS_NODEID)
    probe = _parse_collection(run(probe_selection + ("--collect-only",)))
    _require_probe_matches_gate(probe, gate)
    _require_gate_probe_outcome(run(probe_selection + _PARALLEL_MODE), len(gate))
    for selection in _WITNESS_SELECTIONS:
        _require_payload_outcome(
            run(selection.selection + selection.mode), len(collected[selection.name])
        )


@pytest.mark.slow
@pytest.mark.serial(reason="runs a nested broad pytest suite through a real shell subprocess")
@pytest.mark.exhaustive_only
def test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper(tmp_path: Path) -> None:
    """The actual QA prefix runs the whole suite as three proven-disjoint selections.

    Under the closed wrapper environment, the witness first proves that the
    ordinary (four-worker ``worksteal``), serial-plus-stress (one process) and
    packaging (one process) selections partition the broad collection minus this
    exact node, and that a broad gate-only probe collects exactly ``tests/gate``.
    It then runs the probe, requiring every gate node to pass, and runs each
    selection in order, requiring its outcomes to account for its collection
    (#1015, D217).
    """
    root = _executable_gate_root(tmp_path)
    wrapper = render_validation_commands(NativeClaudeRole.QA, str(root))[1]
    environment = _poisoned_gate_environment()
    repository = Path(__file__).parents[1]
    _run_partitioned_witness(
        _retain_failed_child_streams(
            lambda argv: _run_gate_command(
                f"{wrapper} {shlex.join(argv)}", environment, cwd=repository
            ),
            tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR,
        )
    )


@pytest.mark.slow
@pytest.mark.serial(reason="runs the rendered QA wrapper through a real shell subprocess")
def test_rendered_qa_gate_wrapper_runs_gate_checks_and_broad_collection(tmp_path: Path) -> None:
    """The actual QA prefix runs the gate checks and broadly collects them, without a suite.

    This is the per-pull-request proof that keeps the actual-wrapper sentinel path
    exercised while the nested broad-suite test runs only in exhaustive CI runs.
    """
    root = _executable_gate_root(tmp_path)
    wrapper = render_validation_commands(NativeClaudeRole.QA, str(root))[1]
    repository = Path(__file__).parents[1]

    gate = _run_gate_command(f"{wrapper} tests/gate", _poisoned_gate_environment(), cwd=repository)
    gate_output = gate.stdout + gate.stderr
    assert gate.returncode == 0, gate_output
    assert "validation gate sentinel is absent" not in gate_output
    passed = re.search(r"\b(\d+) passed\b", gate_output)
    assert passed is not None, gate_output
    assert int(passed.group(1)) >= 1

    # pyproject ``addopts`` already carries ``-q``: one level lists node IDs, while an
    # extra ``-q`` (``-qq``) would print only per-file counts.
    collected = _run_gate_command(
        f"{wrapper} tests --collect-only", _poisoned_gate_environment(), cwd=repository
    )
    collected_output = collected.stdout + collected.stderr
    assert collected.returncode == 0, collected_output
    nodeids = {line.strip() for line in collected.stdout.splitlines() if "::" in line}
    gate_nodeids = {
        nodeid for nodeid in nodeids if nodeid.startswith("tests/gate/test_gate_environment.py::")
    }
    listing = _run_gate_command(
        f"{wrapper} tests/gate --collect-only", _poisoned_gate_environment(), cwd=repository
    )
    assert listing.returncode == 0, listing.stdout + listing.stderr
    expected_gate_nodeids = {
        line.strip()
        for line in listing.stdout.splitlines()
        if line.strip().startswith("tests/gate/test_gate_environment.py::")
    }
    assert expected_gate_nodeids
    assert gate_nodeids == expected_gate_nodeids


# --- #1015 nested-witness partition: fast and real-child behavioural tests (D217) ---

_NW_GATE = "tests/gate/test_gate_environment.py::test_gate_environment_is_scrubbed_and_complete"
_NW_A = "tests/test_alpha.py::test_one"
_NW_B = "tests/test_alpha.py::test_two[x y]"
_NW_S = "tests/test_serial_child.py::test_serial"
_NW_P = "tests/test_packaging.py::test_wheel"
_NW_WITNESS = (
    "tests/test_capture_agent_usage.py::"
    "test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper"
)
_NW_BROAD = (_NW_GATE, _NW_A, _NW_B, _NW_S, _NW_WITNESS, _NW_P)
_NW_PROBE = (
    "tests",
    "-k",
    "test_gate_environment_is_scrubbed_and_complete",
    "--deselect",
    _NW_WITNESS,
)
_NW_ORDINARY = (
    "tests",
    "-m",
    "not serial and not stress",
    "--ignore=tests/test_packaging.py",
    "--deselect",
    _NW_WITNESS,
)
_NW_SERIAL = (
    "tests",
    "-m",
    "serial or stress",
    "--ignore=tests/test_packaging.py",
    "--deselect",
    _NW_WITNESS,
)
_NW_PACKAGING = ("tests/test_packaging.py", "--deselect", _NW_WITNESS)
_NW_COLLECT = ("--collect-only",)
_NW_WORKERS = ("-n", "4", "--dist", "worksteal")
# The exact child sequence of a successful run (an independent oracle for the order).
_NW_EXPECTED_CALLS: tuple[tuple[str, ...], ...] = (
    ("tests", "--collect-only"),
    _NW_ORDINARY + _NW_COLLECT,
    _NW_SERIAL + _NW_COLLECT,
    _NW_PACKAGING + _NW_COLLECT,
    ("tests/gate", "--collect-only"),
    _NW_PROBE + _NW_COLLECT,
    _NW_PROBE + _NW_WORKERS,
    _NW_ORDINARY + _NW_WORKERS,
    _NW_SERIAL,
    _NW_PACKAGING,
)


def _nw_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Return a canned pytest child result."""
    return subprocess.CompletedProcess(
        args=("pytest",), returncode=returncode, stdout=stdout, stderr=""
    )


def _nw_collection(nodes: tuple[str, ...], deselected: int = 0, footer: str = "") -> str:
    """Render ``--collect-only`` stdout under pyproject's single ``-q``."""
    if deselected:
        total = len(nodes) + deselected
        trailer = f"{len(nodes)}/{total} tests collected ({deselected} deselected) in 0.01s"
    else:
        noun = "test" if len(nodes) == 1 else "tests"
        trailer = f"{len(nodes)} {noun} collected in 0.01s"
    return "\n".join(nodes) + "\n\n" + footer + trailer + "\n"


def _nw_execution(trailer: str) -> str:
    """Render execution stdout with progress and durations noise before the trailer."""
    return (
        "..s.                                                     [100%]\n"
        "============================= slowest 25 durations ==========\n"
        "1.20s call     tests/test_alpha.py::test_one\n"
        "\n"
        f"{trailer}\n"
        "\n"
    )


def _nw_default_outputs() -> dict[tuple[str, ...], subprocess.CompletedProcess[str]]:
    """Return canned outputs for one fully successful partitioned witness run."""
    return {
        _NW_EXPECTED_CALLS[0]: _nw_completed(_nw_collection(_NW_BROAD)),
        _NW_EXPECTED_CALLS[1]: _nw_completed(_nw_collection((_NW_GATE, _NW_A, _NW_B), 2)),
        _NW_EXPECTED_CALLS[2]: _nw_completed(_nw_collection((_NW_S,), 4)),
        _NW_EXPECTED_CALLS[3]: _nw_completed(_nw_collection((_NW_P,))),
        _NW_EXPECTED_CALLS[4]: _nw_completed(_nw_collection((_NW_GATE,))),
        _NW_EXPECTED_CALLS[5]: _nw_completed(_nw_collection((_NW_GATE,), 5)),
        _NW_EXPECTED_CALLS[6]: _nw_completed(_nw_execution("1 passed in 0.50s")),
        _NW_EXPECTED_CALLS[7]: _nw_completed(_nw_execution("2 passed, 1 skipped in 1.00s")),
        _NW_EXPECTED_CALLS[8]: _nw_completed(_nw_execution("1 passed in 2.00s")),
        _NW_EXPECTED_CALLS[9]: _nw_completed(_nw_execution("1 skipped in 0.10s")),
    }


def _nw_fake_runner(
    outputs: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]],
) -> tuple[_WitnessRunner, list[tuple[str, ...]]]:
    """Return a recording fake runner serving canned outputs by exact argv."""
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return outputs[argv]

    return run, calls


def _nw_partition() -> dict[str, tuple[str, ...]]:
    """Return a valid partition of ``_NW_BROAD`` minus the witness."""
    return {
        "ordinary": (_NW_GATE, _NW_A, _NW_B),
        "serial-stress": (_NW_S,),
        "packaging": (_NW_P,),
    }


def test_nested_witness_selections_are_pinned_exactly() -> None:
    """T1/G2/G16: selections, modes and self-exclusion are pinned to literal argv."""
    assert _WITNESS_NODEID == _NW_WITNESS
    assert _WITNESS_NODEID.endswith(
        "::" + test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper.__name__
    )
    assert _PARALLEL_MODE == _NW_WORKERS
    assert _BROAD_SELECTION == ("tests",)
    assert _GATE_SELECTION == ("tests/gate",)
    assert _GATE_PREFIX == "tests/gate/"
    assert [(item.name, item.selection, item.mode) for item in _WITNESS_SELECTIONS] == [
        ("ordinary", _NW_ORDINARY, _NW_WORKERS),
        ("serial-stress", _NW_SERIAL, ()),
        ("packaging", _NW_PACKAGING, ()),
    ]
    for item in _WITNESS_SELECTIONS:
        assert item.selection.count("--deselect") == 1
        assert item.selection[item.selection.index("--deselect") + 1] == _WITNESS_NODEID
        assert "-k" not in item.selection
        assert not any(arg.startswith("-k") for arg in item.selection)


def test_nested_witness_partition_accepts_complete_disjoint_union() -> None:
    """T2: a complete, disjoint, self-free partition of broad minus self is accepted."""
    _require_partition(_NW_BROAD, _nw_partition())


def test_nested_witness_partition_rejects_dropped_node() -> None:
    """T3/G6: a broad node that escapes every selection fails closed."""
    collected = _nw_partition()
    collected["ordinary"] = (_NW_GATE, _NW_A)
    with pytest.raises(_WitnessContractError, match="drop"):
        _require_partition(_NW_BROAD, collected)


def test_nested_witness_partition_rejects_extra_node() -> None:
    """T3/G6: a selection node absent from the broad collection fails closed."""
    collected = _nw_partition()
    collected["serial-stress"] = (_NW_S, "tests/test_alpha.py::test_extra")
    with pytest.raises(_WitnessContractError, match="add"):
        _require_partition(_NW_BROAD, collected)


def test_nested_witness_partition_rejects_overlapping_selections() -> None:
    """T4/G5: one node counted by two selections fails closed."""
    collected = _nw_partition()
    collected["serial-stress"] = (_NW_S, _NW_A)
    with pytest.raises(_WitnessContractError, match="overlaps"):
        _require_partition(_NW_BROAD, collected)


def test_nested_witness_partition_rejects_duplicate_within_selection() -> None:
    """T5/G4: a node listed twice in one collection block fails closed."""
    stdout = f"{_NW_A}\n{_NW_B}\n{_NW_A}\n\n3 tests collected in 0.01s\n"
    with pytest.raises(_WitnessContractError, match="duplicate"):
        _parse_collection(_nw_completed(stdout))
    collected = _nw_partition()
    collected["packaging"] = (_NW_P, _NW_P)
    with pytest.raises(_WitnessContractError, match="duplicate"):
        _require_partition(_NW_BROAD, collected)


@pytest.mark.parametrize("name", ["ordinary", "serial-stress", "packaging"])
def test_nested_witness_partition_rejects_empty_selection(name: str) -> None:
    """T6/G3: an empty selection fails even when the union still matches broad."""
    collected = _nw_partition()
    removed = set(collected[name])
    collected[name] = ()
    broad = tuple(node for node in _NW_BROAD if node not in removed)
    with pytest.raises(_WitnessContractError, match="empty"):
        _require_partition(broad, collected)


def test_nested_witness_partition_rejects_self_in_selection() -> None:
    """T7/G2: the witness node inside any selection fails closed."""
    collected = _nw_partition()
    collected["serial-stress"] = (_NW_S, _NW_WITNESS)
    with pytest.raises(_WitnessContractError, match="witness"):
        _require_partition(_NW_BROAD, collected)


def test_nested_witness_partition_rejects_self_missing_from_broad() -> None:
    """T7/G1: a broad collection without the witness node fails closed."""
    broad = tuple(node for node in _NW_BROAD if node != _NW_WITNESS)
    with pytest.raises(_WitnessContractError, match="exactly once"):
        _require_partition(broad, _nw_partition())


def test_nested_witness_partition_rejects_self_twice() -> None:
    """T7/G1: a broad collection listing the witness node twice fails closed."""
    with pytest.raises(_WitnessContractError, match="exactly once"):
        _require_partition((*_NW_BROAD, _NW_WITNESS), _nw_partition())


_NW_WARNINGS_FOOTER = (
    "=============================== warnings summary ===============================\n"
    "tests/test_alpha.py::test_one\n"
    "  /repo/tests/test_alpha.py:2: UserWarning: collect-time\n"
    "\n"
    "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
)


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected", "error"),
    [
        pytest.param(
            "\n".join(f"tests/test_m.py::test_{index}" for index in range(13611))
            + "\n\n13611/13645 tests collected (34 deselected) in 16.31s\n",
            0,
            tuple(f"tests/test_m.py::test_{index}" for index in range(13611)),
            None,
            id="selected-form",
        ),
        pytest.param(
            "tests/test_alpha.py::test_one\n\n1 test collected in 0.01s\n",
            0,
            ("tests/test_alpha.py::test_one",),
            None,
            id="singular",
        ),
        pytest.param(
            "tests/test_alpha.py::test_one\ntests/test_alpha.py::test_two[x y]\n\n"
            "2 tests collected in 767.50s (0:12:47)\n",
            0,
            ("tests/test_alpha.py::test_one", "tests/test_alpha.py::test_two[x y]"),
            None,
            id="clock-parametrised-space",
        ),
        pytest.param(
            "tests/test_alpha.py::test_one\ntests/test_alpha.py::test_two[x y]\n\n"
            + _NW_WARNINGS_FOOTER
            + "2 tests collected in 0.01s\n",
            0,
            ("tests/test_alpha.py::test_one", "tests/test_alpha.py::test_two[x y]"),
            None,
            id="warnings-footer",
        ),
        pytest.param(
            f"{_NW_A}\n\n1 test collected in 0.01s\n",
            2,
            None,
            "collection exited nonzero",
            id="nonzero-exit",
        ),
        pytest.param(
            f"{_NW_A}\n1 test collected in 0.01s\n",
            0,
            None,
            "collection has no blank separator",
            id="no-separator",
        ),
        pytest.param(
            "\n1 test collected in 0.01s\n",
            0,
            None,
            "collection node block is empty",
            id="empty-block",
        ),
        pytest.param(
            f" {_NW_A}\n\n1 test collected in 0.01s\n",
            0,
            None,
            "collection line is not a node ID",
            id="leading-space",
        ),
        pytest.param(
            f"<Module {_NW_A}>\n\n1 test collected in 0.01s\n",
            0,
            None,
            "collection line is not a node ID",
            id="non-node",
        ),
        pytest.param(
            f"{_NW_A}\n{_NW_A}\n\n2 tests collected in 0.01s\n",
            0,
            None,
            "collection node block contains a duplicate node ID",
            id="duplicate",
        ),
        pytest.param(
            f"{_NW_A}\n\nno tests collected in 0.01s\n",
            0,
            None,
            "collection trailer is malformed",
            id="no-tests",
        ),
        pytest.param(
            f"{_NW_A}\n\n1 test collected, 1 error in 0.01s\n",
            0,
            None,
            "collection trailer is malformed",
            id="error",
        ),
        pytest.param(
            f"{_NW_A}\n{_NW_B}\n\n1 test collected in 0.01s\n",
            0,
            None,
            "collection block has 2 node IDs but the trailer reports 1",
            id="mismatch",
        ),
        pytest.param(
            f"{_NW_A}\n\n1/5 tests collected (1 deselected) in 0.01s\n",
            0,
            None,
            r"collection trailer selected\+deselected != total",
            id="sel-des-tot",
        ),
        pytest.param("", 0, None, "collection has no blank separator", id="empty-stdout"),
    ],
)
def test_nested_witness_collection_parser_grammar(
    stdout: str, returncode: int, expected: tuple[str, ...] | None, error: str | None
) -> None:
    """T8/G4/G9: only the leading node block counts, under a closed trailer grammar.

    Each rejection names the guard it must trip, so masking by another guard fails.
    """
    result = _nw_completed(stdout, returncode)
    if error is not None:
        with pytest.raises(_WitnessContractError, match=error):
            _parse_collection(result)
    else:
        assert _parse_collection(result) == expected


@pytest.mark.parametrize(
    ("trailer", "expected", "outcomes"),
    [
        pytest.param(
            "13595 passed, 16 skipped in 767.50s",
            13611,
            {_PytestOutcome.PASSED: 13595, _PytestOutcome.SKIPPED: 16},
            id="xdist",
        ),
        pytest.param(
            "33 passed, 13612 deselected, 2 warnings in 102.36s",
            33,
            {
                _PytestOutcome.PASSED: 33,
                _PytestOutcome.DESELECTED: 13612,
                _PytestOutcome.WARNING: 2,
            },
            id="deselected-warnings",
        ),
        pytest.param("15 skipped in 0.80s", 15, {_PytestOutcome.SKIPPED: 15}, id="all-skipped"),
        pytest.param(
            "1 passed, 1 xfailed, 1 xpassed in 0.30s (0:00:00)",
            3,
            {
                _PytestOutcome.PASSED: 1,
                _PytestOutcome.XFAILED: 1,
                _PytestOutcome.XPASSED: 1,
            },
            id="xfail-xpass",
        ),
    ],
)
def test_nested_witness_execution_parser_accepts_accepted_outcomes(
    trailer: str, expected: int, outcomes: dict[_PytestOutcome, int]
) -> None:
    """T9: accepted non-failing outcomes account for exactly the collected count."""
    assert _require_payload_outcome(_nw_completed(_nw_execution(trailer)), expected) == outcomes


_NW_UNKNOWN = "execution trailer has unknown outcome token 'rerun'"
_NW_REPEATS = "execution trailer repeats outcome PASSED"
_NW_MALFORMED = "execution trailer is malformed"
_NW_FAILURES = "payload reported failures or errors"


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected", "error"),
    [
        pytest.param(
            _nw_execution("1 passed, 1 rerun in 0.10s"), 0, 2, _NW_UNKNOWN, id="unknown-token"
        ),
        pytest.param(
            _nw_execution("2 rerun in 0.10s"), 0, 2, _NW_UNKNOWN, id="unknown-token-alone"
        ),
        pytest.param(
            _nw_execution("1 passed, 1 passed in 0.10s"), 0, 2, _NW_REPEATS, id="duplicate-token"
        ),
        pytest.param(
            _nw_execution("no tests ran in 0.01s"), 0, 0, _NW_MALFORMED, id="no-tests-ran"
        ),
        pytest.param(
            "..s.                       [100%]\n", 0, 3, _NW_MALFORMED, id="missing-trailer"
        ),
        pytest.param(_nw_execution("3 Passed in 0.10s"), 0, 3, _NW_MALFORMED, id="case"),
        pytest.param("", 0, 0, "child produced no final line", id="empty-stdout"),
        pytest.param(
            _nw_execution("3 passed in 0.10s"),
            1,
            3,
            "execution exited nonzero",
            id="masked-nonzero",
        ),
        pytest.param(
            _nw_execution("2 passed, 1 failed in 0.10s"), 0, 2, _NW_FAILURES, id="contradictory"
        ),
        pytest.param(_nw_execution("2 passed, 1 error in 0.10s"), 0, 2, _NW_FAILURES, id="error"),
        pytest.param(_nw_execution("2 passed, 2 errors in 0.10s"), 0, 2, _NW_FAILURES, id="errors"),
        pytest.param(
            _nw_execution("2 passed in 0.10s"),
            0,
            3,
            "payload accounted for 2 nodes but 3 were collected",
            id="incomplete",
        ),
        pytest.param(
            _nw_execution("4 passed in 0.10s"),
            0,
            3,
            "payload accounted for 4 nodes but 3 were collected",
            id="surplus",
        ),
    ],
)
def test_nested_witness_execution_parser_grammar(
    stdout: str, returncode: int, expected: int, error: str
) -> None:
    """T9/G10-G13: unknown, malformed, masked, failing or miscounted payloads fail closed.

    Each rejection names the guard it must trip, so masking by another guard fails.
    """
    with pytest.raises(_WitnessContractError, match=error):
        _require_payload_outcome(_nw_completed(stdout, returncode), expected)


def test_nested_witness_gate_keyword_grammar() -> None:
    """T10/G8: the keyword is the closed, sorted, de-duplicated gate function set."""
    assert _gate_keyword((_NW_GATE,)) == "test_gate_environment_is_scrubbed_and_complete"
    assert (
        _gate_keyword((f"{_NW_GATE}[a]", f"{_NW_GATE}[b-1]"))
        == "test_gate_environment_is_scrubbed_and_complete"
    )
    assert (
        _gate_keyword(
            (
                "tests/gate/test_gate_environment.py::test_zulu",
                "tests/gate/test_other.py::test_alpha",
            )
        )
        == "test_alpha or test_zulu"
    )
    for rejected in (
        "tests/gate/test_gate_environment.py::TestGate::test_scrubbed",
        _NW_A,
        "tests/gate/test_gate_environment.py::test_Upper",
        "tests/gate/test_gate_environment.py::test_gate[a b]",
        "tests/gate/test_gate_environment.py::test_gate or test_other",
    ):
        with pytest.raises(
            _WitnessContractError,
            match=re.escape(f"gate node ID {rejected!r} is outside the closed grammar"),
        ):
            _gate_keyword((_NW_GATE, rejected))
    with pytest.raises(_WitnessContractError, match="gate collection is empty"):
        _gate_keyword(())


@pytest.mark.parametrize(
    ("trailer", "expected", "error"),
    [
        pytest.param(
            "1 skipped in 0.10s", 1, r"\['SKIPPED'\] are not exactly 1 passed", id="skipped"
        ),
        pytest.param(
            "1 xfailed in 0.10s", 1, r"\['XFAILED'\] are not exactly 1 passed", id="xfailed"
        ),
        pytest.param(
            "1 xpassed in 0.10s", 1, r"\['XPASSED'\] are not exactly 1 passed", id="xpassed"
        ),
        pytest.param("1 failed in 0.10s", 1, r"\['FAILED'\] are not exactly 1 passed", id="failed"),
        pytest.param("1 error in 0.10s", 1, r"\['ERROR'\] are not exactly 1 passed", id="error"),
        pytest.param(
            "2 passed in 0.10s", 1, r"\['PASSED'\] are not exactly 1 passed", id="surplus"
        ),
        pytest.param("1 passed in 0.10s", 2, r"\['PASSED'\] are not exactly 2 passed", id="short"),
        pytest.param(
            "1 passed, 1 skipped in 0.10s",
            2,
            r"\['PASSED', 'SKIPPED'\] are not exactly 2 passed",
            id="pass-plus-skip",
        ),
        pytest.param(
            "1 passed in 0.10s", 0, r"\['PASSED'\] are not exactly 0 passed", id="zero-expected"
        ),
        pytest.param("no tests ran in 0.10s", 1, "execution trailer is malformed", id="none"),
    ],
)
def test_nested_witness_gate_probe_rejects_non_passed_outcomes(
    trailer: str, expected: int, error: str
) -> None:
    """T11/G14: only exactly ``expected`` passed gate nodes prove a gate pass.

    Each rejection names the guard it must trip, so masking by another guard fails.
    """
    with pytest.raises(_WitnessContractError, match=error):
        _require_gate_probe_outcome(_nw_completed(_nw_execution(trailer)), expected)


def test_nested_witness_gate_probe_accepts_exact_pass_with_non_node_outcomes() -> None:
    """T11: deselected and warning counts are non-node and do not block a gate pass."""
    result = _nw_completed(_nw_execution("1 passed, 13644 deselected, 2 warnings in 3.10s"))
    _require_gate_probe_outcome(result, 1)


def test_nested_witness_orchestration_order_and_argv_reuse() -> None:
    """T12/G15/G16: every proof precedes execution; execution reuses selection argv."""
    run, calls = _nw_fake_runner(_nw_default_outputs())
    _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS
    executions = [call for call in calls if "--collect-only" not in call]
    collections = [call[:-1] for call in calls if call[-1:] == ("--collect-only",)]
    assert executions == [
        _NW_PROBE + _NW_WORKERS,
        _NW_ORDINARY + _NW_WORKERS,
        _NW_SERIAL,
        _NW_PACKAGING,
    ]
    for execution in executions:
        assert any(execution[: len(collected)] == collected for collected in collections)


_NW_BAD_COLLECTION = _nw_completed("ERROR collecting\n\n1 error in 0.10s\n", 2)


@pytest.mark.parametrize("failing_index", range(6))
def test_nested_witness_runs_nothing_when_a_collection_fails(failing_index: int) -> None:
    """T12/G15: a failing collection child stops the run before any execution."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[failing_index]] = _NW_BAD_COLLECTION
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[: failing_index + 1]


def test_nested_witness_stops_after_broad_collection_without_self() -> None:
    """T12/G1/G15: a broad collection lacking the witness stops after that one call."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[0]] = _nw_completed(
        _nw_collection((_NW_GATE, _NW_A, _NW_B, _NW_S, _NW_P))
    )
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError, match="witness node exactly once"):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[:1]


def test_nested_witness_runs_nothing_when_the_partition_fails() -> None:
    """T12/G6/G15: a dropped node detected by the partition proof blocks all execution."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[1]] = _nw_completed(_nw_collection((_NW_GATE, _NW_A), 3))
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError, match="drop"):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[:4]


@pytest.mark.parametrize("failing_index", [6, 7, 8, 9])
def test_nested_witness_stops_at_first_failing_child(failing_index: int) -> None:
    """T13/G10/G15: a failing probe or payload child stops every later child."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[failing_index]] = _nw_completed(
        _nw_execution("1 failed in 0.10s"), 1
    )
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[: failing_index + 1]


def test_nested_witness_rejects_gate_absent_from_ordinary() -> None:
    """T14/G7: a gate check routed outside the ordinary selection, or none, fails closed."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[1]] = _nw_completed(_nw_collection((_NW_A, _NW_B), 3))
    outputs[_NW_EXPECTED_CALLS[2]] = _nw_completed(_nw_collection((_NW_S, _NW_GATE), 3))
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError, match="absent from the ordinary"):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[:5]
    with pytest.raises(_WitnessContractError, match="empty"):
        _require_gate_in_ordinary((), (_NW_GATE, _NW_A))
    with pytest.raises(_WitnessContractError, match="outside"):
        _require_gate_in_ordinary((_NW_A,), (_NW_GATE, _NW_A))


def test_nested_witness_rejects_probe_set_mismatch() -> None:
    """T14/G8: a probe collecting an extra or a different node fails before execution."""
    outputs = _nw_default_outputs()
    extra = "tests/test_alpha.py::test_gate_environment_is_scrubbed_and_complete_twin[p]"
    outputs[_NW_EXPECTED_CALLS[5]] = _nw_completed(_nw_collection((_NW_GATE, extra), 4))
    run, calls = _nw_fake_runner(outputs)
    with pytest.raises(_WitnessContractError, match="beyond the gate set"):
        _run_partitioned_witness(run)
    assert tuple(calls) == _NW_EXPECTED_CALLS[:6]
    with pytest.raises(_WitnessContractError, match="beyond the gate set"):
        _require_probe_matches_gate((_NW_A,), (_NW_GATE,))


def test_nested_witness_body_delegates_to_partitioned_runner() -> None:
    """T18/G17: the witness delegates once, carries no ``-k not`` omission, keeps markers."""
    witness = test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper
    tree = ast.parse(inspect.getsource(witness))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_partitioned_witness"
    ]
    assert len(calls) == 1
    strings = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("not test_" in value or value == "-k" for value in strings)
    marker_names: list[str] = []
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        assert isinstance(target, ast.Attribute)
        marker_names.append(target.attr)
    assert marker_names == ["slow", "serial", "exhaustive_only"]


def _nw_real_child_runner(tmp_path: Path, source: str) -> _WitnessRunner:
    """Write a disposable pytest project and return a runner for real child pytest runs.

    The project's ``addopts`` is copied byte-for-byte from this repository's
    ``pyproject.toml`` so the child's ``-q`` output shape matches the wrapper's.
    """
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    addopts = cast(str, pyproject["tool"]["pytest"]["ini_options"]["addopts"])
    ini = tmp_path / "pytest.ini"
    ini.write_text(
        "[pytest]\n"
        f"addopts = {addopts}\n"
        "markers =\n"
        "    serial: real-child fixture marker\n"
        "    stress: real-child fixture marker\n"
        "    slow: real-child fixture marker\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_child.py").write_text(source)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "PYTEST_ADDOPTS" and not name.startswith("PYTEST_XDIST_")
    }

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "pytest", "-c", str(ini), "-p", "no:cacheprovider"]
        return subprocess.run(
            [*command, *argv],
            capture_output=True,
            text=True,
            env=environment,
            cwd=tmp_path,
            check=False,
        )

    return run


@pytest.mark.slow
def test_nested_witness_real_failing_child_fails_closed(tmp_path: Path) -> None:
    """T15/G10/G12: a real failing child fails closed, even with its exit masked to 0."""
    run = _nw_real_child_runner(
        tmp_path, "def test_pass():\n    pass\n\n\ndef test_fail():\n    assert False\n"
    )
    expected = len(_parse_collection(run(("tests", "--collect-only"))))
    assert expected == 2
    result = run(("tests",))
    assert result.returncode == 1, result.stdout + result.stderr
    with pytest.raises(_WitnessContractError, match="nonzero"):
        _require_payload_outcome(result, expected)
    masked = subprocess.CompletedProcess(result.args, 0, result.stdout, result.stderr)
    with pytest.raises(_WitnessContractError, match="failures or errors"):
        _require_payload_outcome(masked, expected)


# Real-child xdist scheduling fixture (two workers keep the disposable children cheap).
_NW_CHILD_WORKERS = ("-n", "2", "--dist", "worksteal")

_NW_ACCEPTED_SOURCE = """import pytest


def test_pass():
    pass


def test_skip():
    pytest.skip("visibly skipped")


@pytest.mark.xfail(reason="expected failure")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="non-strict xpass")
def test_xpass():
    pass
"""


@pytest.mark.slow
def test_nested_witness_real_accepted_outcomes_and_xdist_trailer(tmp_path: Path) -> None:
    """T16: real pass/skip/xfail/xpass is accepted single-process and under xdist."""
    run = _nw_real_child_runner(tmp_path, _NW_ACCEPTED_SOURCE)
    collected = _parse_collection(run(("tests", "--collect-only")))
    assert len(collected) == 4
    expected = {
        _PytestOutcome.PASSED: 1,
        _PytestOutcome.SKIPPED: 1,
        _PytestOutcome.XFAILED: 1,
        _PytestOutcome.XPASSED: 1,
    }
    for mode in ((), _NW_CHILD_WORKERS):
        outcomes = _require_payload_outcome(run(("tests", *mode)), len(collected))
        assert {key: value for key, value in outcomes.items() if key not in _NON_NODE_OUTCOMES} == (
            expected
        )
    deselected = run(("tests", "--deselect", "tests/test_child.py::test_pass", "--collect-only"))
    assert _parse_collection(deselected) == tuple(
        node for node in collected if node != "tests/test_child.py::test_pass"
    )
    assert "3/4 tests collected (1 deselected)" in deselected.stdout


@pytest.mark.slow
def test_nested_witness_real_skipped_or_xfailing_gate_probe_rejected(tmp_path: Path) -> None:
    """T17/G14: a real skipped or xfailing gate node never proves a gate pass under xdist."""
    run = _nw_real_child_runner(
        tmp_path,
        "import pytest\n\n\ndef test_gate_passes():\n    pass\n\n\n"
        "def test_gate_skips():\n    pytest.skip('gate skipped')\n\n\n"
        "@pytest.mark.xfail(reason='gate xfails')\ndef test_gate_xfails():\n    assert False\n",
    )
    _require_gate_probe_outcome(run(("tests", "-k", "test_gate_passes", *_NW_CHILD_WORKERS)), 1)
    for name in ("test_gate_skips", "test_gate_xfails"):
        result = run(("tests", "-k", name, *_NW_CHILD_WORKERS))
        assert result.returncode == 0, result.stdout + result.stderr
        with pytest.raises(_WitnessContractError, match="not exactly 1 passed"):
            _require_gate_probe_outcome(result, 1)


def test_rendered_pyright_gate_runs_cold_under_the_closed_environment(tmp_path: Path) -> None:
    """T18: the byte-exact pyright gate uses its venv-contained Node runtime."""
    root = _executable_gate_root(tmp_path)
    cwd = tmp_path / "typed-module"
    cwd.mkdir()
    (cwd / "typed_module.py").write_text("answer: int = 42\n")
    command = render_validation_commands(NativeClaudeRole.QA, str(root))[2]
    result = _run_gate_command(command, _poisoned_gate_environment(), cwd=cwd)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not any(marker in output.lower() for marker in ("nodeenv", "download", "network"))
    worktree_gate_recipe._assert_no_poisoned_values(  # pyright: ignore[reportPrivateUsage]
        output
    )
    assert "--disable-warnings" not in output


def test_pyright_node_runtime_is_canonically_contained_in_the_bound_venv(
    tmp_path: Path,
) -> None:
    """T19: package Node, sibling-prefix, and symlink paths fail closed by components."""
    root = _executable_gate_root(tmp_path)
    source = (
        "import os, sys, sysconfig; from pathlib import Path; import nodejs_wheel; "
        "prefix = Path(sys.prefix).resolve(); "
        "purelib = Path(sysconfig.get_path('purelib')).resolve(); "
        "package = Path(nodejs_wheel.__file__).resolve().parent; "
        "node = (package / 'bin' / 'node').resolve(); "
        "within = lambda path: Path(path).resolve().is_relative_to(prefix); "
        "sibling = prefix.parent / (prefix.name + '-evil') / 'bin' / 'node'; "
        "escape = Path(os.environ['TMPDIR']) / 'escaped-node'; "
        "escape_target = Path('/bin/sh').resolve(); "
        "fixture_ready = (escape_target.is_file() and os.access(escape_target, os.X_OK) "
        "and not escape_target.is_relative_to(prefix)); "
        "fixture_ready or (_ for _ in ()).throw(SystemExit(1)); "
        "escape.symlink_to(escape_target); "
        "ok = (purelib.is_relative_to(prefix) and package.is_relative_to(purelib) "
        "and node.is_file() and os.access(node, os.X_OK) and within(node) "
        "and not within(sibling) and not within(escape)); "
        "raise SystemExit(0 if ok else 1)"
    )
    interpreter = shlex.quote(str(root / "venv" / "bin" / "python"))
    command = f"{render_gate_scrub_prefix(str(root))} {interpreter} -c {shlex.quote(source)}"
    result = _run_gate_command(command, _poisoned_gate_environment())
    assert result.returncode == 0, result.stdout + result.stderr


def test_pyright_nodejs_wheel_provider_contract_is_canonically_contained_in_the_bound_venv(
    tmp_path: Path,
) -> None:
    """T22: the pinned ``nodejs_wheel`` provider contract is venv-contained."""
    root = _executable_gate_root(tmp_path)
    source = (
        "import os, sys, sysconfig; from pathlib import Path; import nodejs_wheel; "
        "prefix = Path(sys.prefix).resolve(); "
        "purelib = Path(sysconfig.get_path('purelib')).resolve(); "
        "package = Path(nodejs_wheel.__file__).resolve().parent; "
        "node = (package / 'bin' / 'node').resolve(); "
        "ok = (purelib.is_relative_to(prefix) and package.is_relative_to(purelib) "
        "and node.is_file() and os.access(node, os.X_OK) and node.is_relative_to(prefix)); "
        "raise SystemExit(0 if ok else 1)"
    )
    interpreter = shlex.quote(str(root / "venv" / "bin" / "python"))
    command = f"{render_gate_scrub_prefix(str(root))} {interpreter} -c {shlex.quote(source)}"
    result = _run_gate_command(command, _poisoned_gate_environment())
    assert result.returncode == 0, result.stdout + result.stderr


def test_pyright_nodejs_dependency_is_exactly_pinned() -> None:
    """T20: the checker and venv-contained Node provider stay exact-pinned."""
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    dependencies = pyproject["dependency-groups"]["dev"]
    for package in ("pyright[nodejs]", "nodejs-wheel-binaries"):
        matching_dependencies = [
            dependency for dependency in dependencies if dependency.startswith(package)
        ]
        assert len(matching_dependencies) == 1
        assert re.fullmatch(rf"{re.escape(package)}==[^=<>!~\s]+", matching_dependencies[0])


def test_installed_nodejs_wheel_binaries_matches_the_exact_declaration(tmp_path: Path) -> None:
    """T23: the closed child rejects a validation root provisioned off the declared pin."""
    root = _executable_gate_root(tmp_path)
    source = (
        "import importlib.metadata, tomllib; from pathlib import Path; "
        "pyproject = tomllib.loads(Path('pyproject.toml').read_text()); "
        "dependencies = pyproject['dependency-groups']['dev']; "
        "declared = next(dependency.split('==', 1)[1] for dependency in dependencies "
        "if dependency.startswith('nodejs-wheel-binaries==')); "
        "installed = importlib.metadata.version('nodejs-wheel-binaries'); "
        "raise SystemExit(0 if installed == declared else 1)"
    )
    interpreter = shlex.quote(str(root / "venv" / "bin" / "python"))
    command = f"{render_gate_scrub_prefix(str(root))} {interpreter} -c {shlex.quote(source)}"
    result = _run_gate_command(command, _poisoned_gate_environment(), cwd=Path(__file__).parents[1])
    assert result.returncode == 0, result.stdout + result.stderr


def test_closed_gate_path_is_the_literal_minimum_allowlist() -> None:
    """T21: the rendered environment cannot gain a host Node search path."""
    assert dict(render_gate_environment("/r"))["PATH"] == "/r/venv/bin:/usr/bin:/bin"


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
    "flag",
    [
        "--allowed-tools",
        "--allowedTools",
        "--tools",
        "--permission-mode",
        "--add-dir",
        "--scrub",
        "--verify",
        "--environment",
    ],
)
def test_no_caller_permission_surface_options_exist_on_native(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """T10: no permission-surface option is caller-facing on ``run-native-claude``."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args([*_native_cli_args(role="qa"), flag, "value"])
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag",
    [
        "--allowed-tools",
        "--allowedTools",
        "--tools",
        "--permission-mode",
        "--add-dir",
        "--scrub",
        "--verify",
        "--environment",
    ],
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


@pytest.mark.parametrize("flag", ["--settings", "--restricted", "--agents", "--setting-sources"])
def test_no_caller_read_confinement_option_exists(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """D219 U6: no caller option can select, widen, or disable the read confinement.

    Every required ``story-planner`` argument is supplied so the failure is
    specifically the unrecognized ``flag``.
    """
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [
                *_native_cli_args(
                    role="story-planner", plan_root=_STUB_PLAN_ROOT, plan_sha=_STUB_PLAN_SHA
                ),
                flag,
                "value",
            ]
        )
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


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
    "role",
    [
        "safety-reviewer",
        "security-reviewer",
        "ui-reviewer",
        "repair",
        "engineer-be ",
        "Engineer-BE",
        "engineer_be",
        "x",
    ],
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
        "engineer-be": ("claude-opus-5-5", "medium", RoleCapability.WRITE),
        "engineer-fe": ("claude-opus-5-5", "medium", RoleCapability.WRITE),
        "mcp-contract-checker": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "planning-architect": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "pr-triage": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "product-auditor": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "qa": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "sim-roast-runner": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
        "story-planner": ("claude-opus-5-5", "medium", RoleCapability.READ_ONLY),
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
        "safety-reviewer": (
            "its mandatory whole-diff safety review requires ordinary-role Bash/git access"
        ),
        "security-reviewer": (
            "its mandatory whole-diff security review requires ordinary-role Bash/git access"
        ),
        "ui-reviewer": (
            "its Playwright MCP conflicts with the empty-MCP, empty-tools native"
            " capture launch boundary"
        ),
    }
    for reason in NATIVE_ROLE_EXCLUSIONS.values():
        assert isinstance(reason, str) and reason.strip()


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        (b"model: claude-opus-5-5", b"model: claude-fable-5"),
        (b"effort: medium", b"effort: unsupported"),
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


@pytest.mark.parametrize(
    "role",
    [
        NativeClaudeRole.PR_TRIAGE,
        NativeClaudeRole.STORY_PLANNER,
    ],
)
def test_shell_free_native_roles_reject_bash_frontmatter_drift(
    role: NativeClaudeRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future role-file edit cannot silently grant Bash to shell-free assurance."""
    path = Path(".claude") / "agents" / f"{role.value}.md"
    content = path.read_bytes().replace(
        b"tools: Read, Grep, Glob\n", b"tools: Read, Grep, Glob, Bash\n", 1
    )

    def mutated_input(_path: Path) -> bytes:
        return content

    monkeypatch.setattr(usage_cli, "_input_bytes", mutated_input)
    with pytest.raises(CaptureUsageError, match="frontmatter is invalid"):
        usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]


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
_STUB_USAGE_ROOT = "/usage/root"


def _native_cli_args(
    role: str = "engineer-be",
    *,
    validation_root: str | None = None,
    plan_root: str | None = None,
    plan_sha: str | None = None,
    evidence_root: str | None = None,
    evidence_pr: int | None = None,
    usage_root: str = _STUB_USAGE_ROOT,
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
        "--usage-root",
        usage_root,
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
    """Return the closed parent fixture bound to one session and the live engineer-be pin.

    The fixture was recorded at ``claude-sonnet-5``/``high``; the launch path binds
    the transcript to the committed pin, so the recorded model and effort are
    rebound to it exactly like the session id.
    """
    pin = usage_cli._native_role_pin(NativeClaudeRole.ENGINEER_BE)  # pyright: ignore[reportPrivateUsage]
    return (
        (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl")
        .read_bytes()
        .replace(b"11111111-1111-4111-8111-111111111233", session_id.encode())
        .replace(b'"model":"claude-sonnet-5"', f'"model":"{pin.model}"'.encode())
        .replace(b'"effort":"high"', f'"effort":"{pin.effort}"'.encode())
    )


def _configure_native_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixed_session: bool = True,
    stub_usage_root: bool = True,
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
    if stub_usage_root:

        def fixed_usage_root(raw: str, _bound_root: BoundRoot | None) -> usage_cli._UsageRoot:  # pyright: ignore[reportPrivateUsage]
            assert raw == _STUB_USAGE_ROOT
            descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            return usage_cli._UsageRoot(  # pyright: ignore[reportPrivateUsage]
                path=str(tmp_path), descriptor=descriptor
            )

        monkeypatch.setattr(usage_cli, "_validate_usage_root", fixed_usage_root)
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
    version: str = "2.1.233",
    version_output: bytes | None = None,
) -> Callable[..., _NativeProcess]:
    """Return a fake version/worker launcher that creates only one parent transcript."""

    def fake(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            process = _NativeProcess(
                f"Claude Code {version}\\n".encode() if version_output is None else version_output
            )
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
    """Two native launches use distinct sessions and one external byte-clean usage root."""
    usage_root = tmp_path.parent / f"{tmp_path.name}-usage"
    _mkdir_exact(usage_root, 0o700)
    project, observed = _configure_native_launcher(
        tmp_path, monkeypatch, fixed_session=False, stub_usage_root=False
    )
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
    assert main(_native_cli_args(usage_root=str(usage_root))) == 0
    assert main(_native_cli_args(usage_root=str(usage_root))) == 0

    worker_sessions = [
        argv[argv.index("--session-id") + 1] for argv, _ in observed if "--session-id" in argv
    ]
    assert len(worker_sessions) == len(preflight_sessions) == 2
    assert worker_sessions == preflight_sessions and worker_sessions[0] != worker_sessions[1]
    assert all(UUID(session_id).version == 4 for session_id in worker_sessions)
    records = [
        USAGE_RECORD_ADAPTER.validate_json(line)
        for line in (usage_root / ".agent-usage" / "usage.jsonl").read_text().splitlines()
    ]
    assert [
        record.session_id for record in records if isinstance(record, NativeWorkerUsageRecord)
    ] == worker_sessions
    assert not Path(".agent-usage").exists()
    worker_launches = [(argv, kwargs) for argv, kwargs in observed if "--session-id" in argv]
    assert all(str(usage_root) not in repr((argv, kwargs)) for argv, kwargs in worker_launches)


def test_usage_root_rejects_unsafe_shapes_and_active_root_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native usage roots fail closed on mode, ownership, symlink, and bound-root overlap."""
    monkeypatch.chdir(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-usage-shapes"
    _mkdir_exact(external, 0o700)
    bound_child = external / "bound-child"
    _mkdir_exact(bound_child, 0o700)

    bound = BoundRoot(kind=BoundRootKind.PLAN, path=str(external), reattest=None)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(  # pyright: ignore[reportPrivateUsage]
            str(bound_child), bound
        )
    child_bound = BoundRoot(kind=BoundRootKind.PLAN, path=str(bound_child), reattest=None)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(str(external), child_bound)  # pyright: ignore[reportPrivateUsage]

    inside_worktree = tmp_path / "usage"
    _mkdir_exact(inside_worktree, 0o700)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(  # pyright: ignore[reportPrivateUsage]
            str(inside_worktree), None
        )

    wrong_mode = tmp_path.parent / f"{tmp_path.name}-usage-mode"
    _mkdir_exact(wrong_mode, 0o755)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(  # pyright: ignore[reportPrivateUsage]
            str(wrong_mode), None
        )

    symlink = tmp_path.parent / f"{tmp_path.name}-usage-link"
    symlink.symlink_to(external)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(str(symlink), None)  # pyright: ignore[reportPrivateUsage]

    real_geteuid = os.geteuid
    monkeypatch.setattr(os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(CaptureUsageError, match="usage root is invalid"):
        usage_cli._validate_usage_root(str(external), None)  # pyright: ignore[reportPrivateUsage]


def test_usage_root_rejects_path_replacement_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usage-root inode replacement between launch and append fails closed."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path.parent / f"{tmp_path.name}-usage-replace"
    moved = tmp_path.parent / f"{tmp_path.name}-usage-original"
    _mkdir_exact(root, 0o700)
    usage_root = usage_cli._validate_usage_root(  # pyright: ignore[reportPrivateUsage]
        str(root), None
    )
    try:
        root.rename(moved)
        _mkdir_exact(root, 0o700)
        with pytest.raises(CaptureUsageError, match="usage root is invalid"):
            usage_cli._reattest_usage_root(usage_root)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(usage_root.descriptor)


def test_native_parser_requires_usage_root_and_keeps_output_relative() -> None:
    """Native launch requires an external root but retains the confined relative leaf grammar."""
    args = _native_cli_args()
    index = args.index("--usage-root")
    del args[index : index + 2]
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(args)
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args([*_native_cli_args(), "--output", "/tmp/usage.jsonl"])
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            [*_native_cli_args(), "--output", ".agent-usage/../escape.jsonl"]
        )


def test_native_command_launches_exact_worker_and_records_immutable_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D161 binds a UUID session, fixed argv and stdin to one immutable parent transcript."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setenv("PATH", "/allowed/native-bin")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/host/injected.js")
    monkeypatch.setenv("ROASTPILOT_PROBE_SENTINEL", "hostile")
    monkeypatch.setenv("roastpilot_probe_sentinel", "hostile")
    transcript = _native_transcript_bytes().replace(b'"version":"2.1.233"', b'"version":"2.1.241"')
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=transcript, version="2.1.241"),
    )
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)

    assert main(_native_cli_args()) == 0

    version_launches = [(argv, kwargs) for argv, kwargs in observed if argv[-1] == "--version"]
    worker_launches = [(argv, kwargs) for argv, kwargs in observed if argv[-1] != "--version"]
    assert len(version_launches) == len(worker_launches) == 1
    assert version_launches[0][0] == ["claude", "--version"]
    probe_environment = cast(dict[str, str], version_launches[0][1]["env"])
    assert probe_environment["PATH"] == "/allowed/native-bin"
    assert probe_environment["LANG"] == "en_GB.UTF-8"
    assert "NODE_OPTIONS" not in probe_environment
    assert "ROASTPILOT_PROBE_SENTINEL" not in probe_environment
    assert "roastpilot_probe_sentinel" not in probe_environment
    assert set(probe_environment) <= usage_cli._NATIVE_SAFE_INHERITED_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    argv, kwargs = worker_launches[0]
    expected_argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.ENGINEER_BE,
        RoleCapability.WRITE,
        usage_cli._native_role_pin(NativeClaudeRole.ENGINEER_BE).effort,  # pyright: ignore[reportPrivateUsage]
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
    assert record.harness_version == "2.1.241"
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


def test_generic_claude_probe_uses_closed_environment_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic Claude probes retain only the closed inherited environment."""
    monkeypatch.chdir(tmp_path)
    prompt = tmp_path / "prompt"
    prompt.write_text("prompt")
    monkeypatch.setenv("PATH", "/allowed/bin")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/host/injected.js")
    monkeypatch.setenv("ROASTPILOT_PROBE_SENTINEL", "hostile")
    monkeypatch.setenv("roastpilot_probe_sentinel", "hostile")
    observed: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            return _NativeProcess(b"Claude Code 2.1.241\n")
        return _NativeProcess((FIXTURES / "claude-2.1.241.jsonl").read_bytes())

    def resolved(_family: HarnessFamily) -> str:
        return "/stub/claude"

    monkeypatch.setattr(usage_cli, "_resolved_executable", resolved)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", popen)
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)

    assert (
        main(
            [
                "run",
                "--harness",
                "claude",
                "--prompt-file",
                str(prompt),
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "measurement",
                "--model",
                "synthetic-primary",
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
    probes = [(argv, kwargs) for argv, kwargs in observed if argv[-1] == "--version"]
    assert len(probes) == 1
    probe_environment = cast(dict[str, str], probes[0][1]["env"])
    assert probe_environment["PATH"] == "/allowed/bin"
    assert probe_environment["LANG"] == "en_GB.UTF-8"
    assert "NODE_OPTIONS" not in probe_environment
    assert "ROASTPILOT_PROBE_SENTINEL" not in probe_environment
    assert "roastpilot_probe_sentinel" not in probe_environment
    assert set(probe_environment) <= usage_cli._NATIVE_SAFE_INHERITED_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]


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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            code=2,
            transcript=_proven_story_planner_transcript_bytes(),
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
    live_model = usage_cli._native_role_pin(NativeClaudeRole.ENGINEER_BE).model  # pyright: ignore[reportPrivateUsage]
    mismatched = _native_transcript_bytes().replace(
        f'"model":"{live_model}"'.encode(), b'"model":"claude-sonnet-5"'
    )
    assert mismatched != _native_transcript_bytes()
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=mismatched),
    )

    with pytest.raises(SystemExit, match="native Claude transcript is invalid"):
        main(_native_cli_args())

    assert len(observed) == 1
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
            version = b"Claude Code 2.1.999\n" if failure == "version" else b"Claude Code 2.1.233\n"
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
    assert len(observed) == 2


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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(),
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


def test_open_bound_root_descriptor_closes_after_directory_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor opened before directory validation fails is not leaked."""
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def opened_descriptor(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        del path, flags, mode, dir_fd
        return descriptor

    monkeypatch.setattr(usage_cli.os, "open", opened_descriptor)

    def failing_directory_check(_descriptor: int, *, expect_mode: int | None) -> None:
        del expect_mode
        raise OSError("directory invalid")

    monkeypatch.setattr(usage_cli, "_require_validation_directory", failing_directory_check)
    try:
        with pytest.raises(CaptureUsageError, match="plan root is invalid"):
            usage_cli._open_bound_root_descriptor(  # pyright: ignore[reportPrivateUsage]
                str(tmp_path), error_message="plan root is invalid"
            )
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)


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
        "capture_usage_native_codex.py",
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
def isolate_run_metadata_validation(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep launcher tests provider-free unless they exercise Git admission directly."""

    def skip_worktree_validation(_arguments: argparse.Namespace) -> None:
        return None

    monkeypatch.setattr(usage_cli, "_validate_worktree_metadata", skip_worktree_validation)

    def resolved_executable(harness: HarnessFamily) -> str:
        """Keep supervisor units on a local synthetic executable path."""
        return f"/stub/{harness.value}"

    def harness_version(executable: str) -> str:
        """Return the ratified observed version for the selected harness family."""
        if Path(executable).name.lower() == "codex":
            return "0.147.0"
        if Path(executable).name.lower() == "claude":
            return "2.1.233"
        raise AssertionError(executable)

    def harness_version_probe(executable: str) -> tuple[str, bytes]:
        """Return the stubbed version with Claude Code's observed release spelling."""
        version = harness_version(executable)
        return version, f"{version} (Claude Code)\n".encode()

    lifecycle_tests = {
        "test_version_probe_timeout_kills_reaps_and_never_reaches_a_run",
        "test_provider_free_executable_integration_captures_only_normalized_usage",
        "test_early_closed_stdin_fails_without_appending_valid_terminal_usage",
        "test_real_pipe_backpressure_interleaves_prompt_write_and_stdout_drain",
        "test_run_timeout_kills_and_reaps_term_ignoring_child_without_a_record",
        "test_git_output_timeout_kills_term_ignoring_process_group",
        "test_timeout_kills_real_stdout_inheriting_descendant_without_record",
        "test_native_command_fails_closed_at_transcript_launch_boundaries",
    }
    original_name = cast(
        str,
        request.node.originalname or request.node.name,  # pyright: ignore[reportUnknownMemberType]
    )
    if original_name not in lifecycle_tests:
        monkeypatch.setattr(usage_cli, "_resolved_executable", resolved_executable)
        monkeypatch.setattr(usage_cli, "_harness_version", harness_version)
        monkeypatch.setattr(usage_cli, "_harness_version_probe", harness_version_probe)


def _stream(*events: str) -> BytesIO:
    """Build a binary JSONL stream matching the future fixed subprocess stdout type."""
    return BytesIO("".join(events).encode("utf-8"))


def _parse_claude_lax(stream: BinaryIO) -> ParsedUsage:
    """Parse a sanitized fixture without asserting a live launch boundary."""
    return parse_claude_stream(stream, expected_version="2.1.233", require_launch_authority=False)


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
        expected_version="2.1.233",
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
        usage = parse_claude_stream(
            stream, expected_version="2.1.233", require_launch_authority=True
        )

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
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )
    assert "SANITIZED_MESSAGE" not in str(exc_info.value)


def test_claude_2_1_233_native_fixture_records_observed_stream_and_authority_rejection() -> None:
    """Native stream evidence parses laxly and proves strict authority rejects pre-init hooks."""
    fixture = FIXTURES / "claude-2.1.233-native.jsonl"
    events = [json.loads(line) for line in fixture.read_text().splitlines()]
    with fixture.open("rb") as stream:
        usage = parse_claude_stream(
            stream, expected_version="2.1.233", require_launch_authority=False
        )

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
        parse_claude_stream(stream, expected_version="2.1.233", require_launch_authority=True)


def test_claude_retired_stream_event_type_fails_closed() -> None:
    """``user`` is retired: no supplied 2.1.233 fixture observes it as a stream event."""
    with pytest.raises(ClaudeUsageParseError, match="unknown Claude event type"):
        parse_claude_stream(
            _stream('{"type":"user"}\n'), expected_version="2.1.233", require_launch_authority=False
        )


def test_claude_retired_default_permission_mode_fails_closed() -> None:
    """``default`` is retired: every supplied 2.1.233 init observes ``plan`` only."""
    init = json.loads((FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()[0])
    init["permissionMode"] = "default"
    for strict in (False, True):
        with pytest.raises(ClaudeAuthorityError, match="init authority is malformed"):
            parse_claude_stream(
                _stream(json.dumps(init) + "\n"),
                expected_version="2.1.233",
                require_launch_authority=strict,
            )


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
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


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
            parse_claude_stream(
                BytesIO(legacy), expected_version="2.1.233", require_launch_authority=strict
            )

    for field, value in (("tools", ["SENTINEL_TOOL"]), ("mcp_servers", ["SENTINEL_MCP"])):
        drifted = {**init, field: value}
        with pytest.raises(ClaudeAuthorityError) as error:
            parse_claude_stream(
                _stream(json.dumps(drifted) + "\n", json.dumps(terminal) + "\n"),
                expected_version="2.1.233",
                require_launch_authority=True,
            )
        assert "SENTINEL" not in str(error.value)

    for field, value in (("permissionMode", "unknown-SENTINEL"), ("permissionMode", 1)):
        malformed = {**init, field: value}
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError) as error:
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    expected_version="2.1.233",
                    require_launch_authority=strict,
                )
            assert "SENTINEL" not in str(error.value)

    for field, value in (("tools", None), ("mcp_servers", "none"), ("permissionMode", None)):
        malformed = {key: item for key, item in init.items() if key != field}
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError):
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    expected_version="2.1.233",
                    require_launch_authority=strict,
                )
        malformed[field] = value
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError):
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    expected_version="2.1.233",
                    require_launch_authority=strict,
                )

    duplicate = _stream(
        json.dumps(init) + "\n", json.dumps(init) + "\n", json.dumps(terminal) + "\n"
    )
    for strict in (False, True):
        with pytest.raises(ClaudeAuthorityError):
            parse_claude_stream(
                BytesIO(duplicate.getvalue()),
                expected_version="2.1.233",
                require_launch_authority=strict,
            )
    assert (
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        ).input_tokens
        == 2
    )
    with pytest.raises(ClaudeAuthorityError):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=True,
        )
    with pytest.raises(ClaudeAuthorityError):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n", json.dumps(init) + "\n"),
            expected_version="2.1.233",
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
            parse_claude_stream(
                BytesIO(stream.getvalue()),
                expected_version="2.1.233",
                require_launch_authority=True,
            )
        assert str(error.value) == "Claude init authority is not attested"
        assert "PRE_INIT_SENTINEL" not in str(error.value)
        assert (
            parse_claude_stream(
                BytesIO(stream.getvalue()),
                expected_version="2.1.233",
                require_launch_authority=False,
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
            _stream(json.dumps({"type": sentinel}) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )
    assert sentinel not in str(claude_type_error.value)

    with pytest.raises(ClaudeUsageParseError) as claude_subtype_error:
        parse_claude_stream(
            _stream(json.dumps({"type": "system", "subtype": sentinel}) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )
    assert sentinel not in str(claude_subtype_error.value)


def test_malformed_or_missing_terminal_usage_fails_closed() -> None:
    """A zero-exit-like stream without required terminal usage cannot normalize."""
    with pytest.raises(CodexUsageParseError, match="terminal"):
        parse_codex_stream(_stream('{"type":"turn.started"}\n'))
    with pytest.raises(ClaudeUsageParseError, match="terminal"):
        parse_claude_stream(
            _stream('{"type":"assistant"}\n'),
            expected_version="2.1.233",
            require_launch_authority=False,
        )
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
        parse_claude_stream(
            BytesIO(b"\xff\n"), expected_version="2.1.233", require_launch_authority=False
        )
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
            '{"type":"system","payload":"JSON_SENTINEL",}', expected_version="2.1.233"
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
        parse_claude_stream(
            BytesIO(b'{"k":"E2E_SENTINEL"\xff\n'),
            expected_version="2.1.233",
            require_launch_authority=False,
        )
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
            BytesIO(b'{"type" "MALFORMED_SENTINEL"}\n'),
            expected_version="2.1.233",
            require_launch_authority=False,
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
        parse_claude_stream(
            BytesIO(stream), expected_version="2.1.233", require_launch_authority=False
        )
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
            '{"type":"system","type":"system"}', expected_version="2.1.233"
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


# --- #1026: frozen original Codex root scanner and string fast-path equivalence ---
#
# ``_OriginalJsonRootScanner`` is a verbatim copy of base f3d420d4's
# ``capture_usage_codex.py`` lines 67-330, with only the class name and three
# module constants renamed to the literal-valued oracle constants below, so a
# source-constant mutation cannot move the oracle with it. Equivalence evidence
# is finite (exhaustive short suffixes, seeded corpora, and literal anchors); it
# is not a proof over all inputs, and CPython's ``re`` search is assumed correct.

_ORACLE_STRING_CAPTURE_LIMIT = 64
_ORACLE_JSON_NESTING_DEPTH = 64
_ORACLE_OPAQUE_ITEM_TYPES = frozenset({"item.started", "item.updated", "item.completed"})


class _OriginalJsonRootScanner:
    """Incrementally validate one JSON root object without retaining its payload."""

    def __init__(self) -> None:
        """Initialize a root-object scanner with bounded string token capture."""
        self._stack: list[tuple[str, str, str | None]] = []
        self._root_started = False
        self._root_finished = False
        self._string: list[str] = []
        self._string_truncated = False
        self._string_state = "none"
        self._unicode_digits: list[str] = []
        self._primitive_kind: str | None = None
        self._primitive_state = ""
        self._root_type_count = 0
        self._root_type_value: str | None = None

    @property
    def is_opaque_item(self) -> bool:
        """Return whether this complete root has exactly one allowed item type."""
        return self._root_type_count == 1 and self._root_type_value in _ORACLE_OPAQUE_ITEM_TYPES

    @property
    def has_nonopaque_root_type(self) -> bool:
        """Return whether a root discriminator rules out streamed opaque-item handling."""
        return self._root_type_count == 1 and not self.is_opaque_item

    @property
    def has_root_content(self) -> bool:
        """Return whether the event contains more than JSONL whitespace."""
        return self._root_started

    def consume(self, text: str) -> None:
        """Consume decoded JSON characters without preserving opaque payload values."""
        index = 0
        while index < len(text):
            character = text[index]
            if self._string_state != "none":
                self._consume_string(character)
                index += 1
                continue
            if self._primitive_kind is not None:
                if self._is_delimiter(character):
                    self._finish_primitive()
                    continue
                self._consume_primitive(character)
                index += 1
                continue
            if character in " \t\r":
                index += 1
                continue
            if character == '"':
                self._string_state = "string"
                self._string = []
                self._string_truncated = False
            elif character in "{}[],:":
                self._consume_punctuation(character)
            elif character in "-0123456789":
                self._primitive_kind = "number"
                self._primitive_state = (
                    "minus" if character == "-" else ("zero" if character == "0" else "integer")
                )
            elif character in "tfn":
                self._primitive_kind = "literal"
                self._primitive_state = {"t": "true:1", "f": "false:1", "n": "null:1"}[character]
            else:
                self._reject()
            index += 1

    def finish(self) -> None:
        """Require a complete, single root object at the JSONL newline boundary."""
        if self._string_state != "none":
            self._reject()
        if self._primitive_kind is not None:
            self._finish_primitive()
        if not self._root_started or not self._root_finished or self._stack:
            self._reject()

    def _reject(self) -> None:
        """Raise a content-free structural error for every scanner failure class."""
        raise CodexUsageParseError("malformed Codex JSON event")

    @staticmethod
    def _is_delimiter(character: str) -> bool:
        """Return whether a character terminates a JSON scalar token."""
        return character in " \t\r{}[],:"

    def _append_string_character(self, character: str) -> None:
        """Keep only the bounded string prefix needed for root discriminator checks."""
        if len(self._string) < _ORACLE_STRING_CAPTURE_LIMIT:
            self._string.append(character)
        else:
            self._string_truncated = True

    def _consume_string(self, character: str) -> None:
        """Consume one JSON string character, including strictly validated escapes."""
        if self._string_state == "string":
            if character == '"':
                self._string_state = "none"
                self._emit("string", "".join(self._string), self._string_truncated)
            elif character == "\\":
                self._string_state = "escape"
            elif ord(character) < 0x20:
                self._reject()
            else:
                self._append_string_character(character)
            return
        if self._string_state == "escape":
            escaped = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }.get(character)
            if escaped is not None:
                self._append_string_character(escaped)
                self._string_state = "string"
            elif character == "u":
                self._unicode_digits = []
                self._string_state = "unicode"
            else:
                self._reject()
            return
        if character not in "0123456789abcdefABCDEF":
            self._reject()
        self._unicode_digits.append(character)
        if len(self._unicode_digits) == 4:
            self._append_string_character(chr(int("".join(self._unicode_digits), 16)))
            self._string_state = "string"

    def _consume_primitive(self, character: str) -> None:
        """Validate a JSON literal or number with a bounded deterministic automaton."""
        if self._primitive_kind == "literal":
            expected, position = self._primitive_state.split(":")  # pyright: ignore[reportOptionalMemberAccess]
            offset = int(position)
            if offset >= len(expected) or character != expected[offset]:
                self._reject()
            self._primitive_state = f"{expected}:{offset + 1}"
            return
        transitions = {
            "minus": {"digit": "zero_or_integer"},
            "zero": {".": "fraction_start", "eE": "exponent_start"},
            "integer": {"digit": "integer", ".": "fraction_start", "eE": "exponent_start"},
            "fraction_start": {"digit": "fraction"},
            "fraction": {"digit": "fraction", "eE": "exponent_start"},
            "exponent_start": {"+-": "exponent_sign", "digit": "exponent"},
            "exponent_sign": {"digit": "exponent"},
            "exponent": {"digit": "exponent"},
        }
        state = self._primitive_state
        if character in "0123456789":
            category = "digit"
        elif character == ".":
            category = character
        elif character in "+-":
            category = "+-"
        elif character in "eE":
            category = "eE"
        else:
            self._reject()
            return
        next_state = transitions.get(state, {}).get(category)
        if next_state == "zero_or_integer":
            next_state = "zero" if character == "0" else "integer"
        if next_state is None:
            self._reject()
        self._primitive_state = next_state

    def _finish_primitive(self) -> None:
        """Emit only JSON scalars that ended in an accepting state."""
        assert self._primitive_kind is not None
        if self._primitive_kind == "literal":
            expected, position = self._primitive_state.split(":")  # pyright: ignore[reportOptionalMemberAccess]
            if int(position) != len(expected):
                self._reject()
        elif self._primitive_state not in {"zero", "integer", "fraction", "exponent"}:
            self._reject()
        self._primitive_kind = None
        self._primitive_state = ""
        self._emit("primitive", None, False)

    def _consume_punctuation(self, character: str) -> None:
        """Consume structural punctuation through the root-only JSON grammar."""
        if character in "[{":
            if not self._expect_value():
                self._reject()
            if len(self._stack) >= _ORACLE_JSON_NESTING_DEPTH:
                self._reject()
            if character == "{":
                self._stack.append(("object", "key_or_end", None))
            else:
                self._stack.append(("array", "value_or_end", None))
            self._root_started = True
            return
        if character in "]}":
            self._close_container(character)
            return
        if character == ":":
            if not self._stack or self._stack[-1][0] != "object" or self._stack[-1][1] != "colon":
                self._reject()
            kind, _state, key = self._stack.pop()
            self._stack.append((kind, "value", key))
            return
        if character == ",":
            if not self._stack:
                self._reject()
            kind, state, _key = self._stack.pop()
            if state != "comma_or_end":
                self._reject()
            self._stack.append((kind, "key" if kind == "object" else "value", None))
            return
        self._reject()

    def _expect_value(self) -> bool:
        """Return whether the current parser location admits a complete JSON value."""
        if not self._root_started:
            return True
        if not self._stack:
            return False
        return self._stack[-1][1] in {"value", "value_or_end"}

    def _emit(self, kind: str, value: str | None, truncated: bool) -> None:
        """Feed a completed scalar token into the enclosing object or array state."""
        if not self._root_started or not self._stack:
            self._reject()
        container, state, key = self._stack.pop()
        if container == "object" and state in {"key_or_end", "key"}:
            if kind != "string":
                self._reject()
            self._stack.append((container, "colon", value if not truncated else None))
            return
        if state not in {"value", "value_or_end"}:
            self._reject()
        if len(self._stack) == 0 and key == "type":
            self._root_type_count += 1
            if self._root_type_count != 1:
                raise CodexUsageParseError("Codex event contains duplicate JSON keys")
            if kind != "string":
                self._reject()
            self._root_type_value = None if truncated else value
        self._stack.append((container, "comma_or_end", None))

    def _close_container(self, character: str) -> None:
        """Close an object or array only from legal states and propagate its value."""
        if not self._stack:
            self._reject()
        container, state, _key = self._stack[-1]
        expected = "}" if container == "object" else "]"
        if character != expected or state not in {"key_or_end", "value_or_end", "comma_or_end"}:
            self._reject()
        self._stack.pop()
        if not self._stack:
            self._root_finished = True
            return
        parent, parent_state, parent_key = self._stack.pop()
        if parent_state not in {"value", "value_or_end"}:
            self._reject()
        if len(self._stack) == 0 and parent_key == "type":
            self._reject()
        self._stack.append((parent, "comma_or_end", None))


_ORIGINAL_SCANNER_SOURCE_SHA256 = "6da9c3f2ca686c30f498e8fa7c943799e215a8524637168d57cda9f4a9a8ba57"
_SCANNER_STATE_FIELDS = (
    "_stack",
    "_root_started",
    "_root_finished",
    "_string",
    "_string_truncated",
    "_string_state",
    "_unicode_digits",
    "_primitive_kind",
    "_primitive_state",
    "_root_type_count",
    "_root_type_value",
)
_OPAQUE_STRING_PREFIX = '{"type":"item.updated","payload":"'


def _candidate_scanner_class() -> type[Any]:
    """Resolve the production scanner at call time so a source mutant is observed."""
    return usage_codex._JsonRootScanner  # pyright: ignore[reportPrivateUsage]


def _scanner_snapshot(scanner: Any) -> tuple[object, ...]:
    """Return a detached copy of every scanner field plus its public classifications."""
    state = cast(dict[str, object], vars(scanner))
    assert len(_SCANNER_STATE_FIELDS) == 11
    assert set(state) == set(_SCANNER_STATE_FIELDS), (
        f"scanner instance fields changed: {sorted(set(state) ^ set(_SCANNER_STATE_FIELDS))}"
    )
    values: list[object] = []
    for field in _SCANNER_STATE_FIELDS:
        value: object = state[field]
        values.append(tuple(cast(list[object], value)) if isinstance(value, list) else value)
    values.extend(
        (scanner.is_opaque_item, scanner.has_nonopaque_root_type, scanner.has_root_content)
    )
    return tuple(values)


def _exception_observable(error: Exception) -> tuple[object, ...]:
    """Return the comparable type, text, arguments, and chaining of one raised error."""
    return (
        type(error),
        str(error),
        error.args,
        error.__cause__ is None,
        error.__context__ is None,
        error.__suppress_context__,
    )


class _RecordingSpecialSearch:
    """Delegate to the production special-character pattern while bounding its work.

    ``begin`` sets the budget for one ``consume`` call: within one call the searched
    spans must be disjoint, so their sum can never exceed the characters supplied.
    A rescanning search therefore fails this assertion in finite time instead of
    looping.
    """

    def __init__(self, pattern: re.Pattern[str]) -> None:
        """Wrap the real compiled pattern with zeroed work counters."""
        self._pattern = pattern
        self.budget = 0
        self.spent = 0
        self.total_spent = 0
        self.calls = 0
        self.skipped_runs = 0

    def begin(self, characters: int) -> None:
        """Start the scan budget for one ``consume`` call of ``characters`` length."""
        self.budget = characters
        self.spent = 0

    def search(self, string: str, pos: int = 0) -> re.Match[str] | None:
        """Search like ``re.Pattern.search`` while recording the scanned span."""
        match = self._pattern.search(string, pos)
        end = len(string) if match is None else match.end()
        self.calls += 1
        self.spent += end - pos
        self.total_spent += end - pos
        if match is None or match.start() > pos:
            self.skipped_runs += 1
        assert self.spent <= self.budget, (
            f"special-character search scanned {self.spent} > {self.budget} characters "
            "within one consume call"
        )
        return match


def _install_search_guard(monkeypatch: pytest.MonkeyPatch) -> _RecordingSpecialSearch:
    """Replace the production special-character pattern with a recording proxy."""
    guard = _RecordingSpecialSearch(
        usage_codex._STRING_SPECIAL_CHARACTER  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setattr(usage_codex, "_STRING_SPECIAL_CHARACTER", guard)
    return guard


def _observe_scanner(
    scanner_class: type[Any], pieces: list[str], guard: _RecordingSpecialSearch | None = None
) -> list[tuple[object, ...]]:
    """Feed decoded pieces, recording the error-or-state observable after every call."""
    scanner = scanner_class()
    observed: list[tuple[object, ...]] = []
    for piece in pieces:
        if guard is not None:
            guard.begin(len(piece))
        try:
            scanner.consume(piece)
        except AssertionError:
            raise
        except Exception as error:
            observed.append(("consume", _exception_observable(error), _scanner_snapshot(scanner)))
            return observed
        observed.append(("consume", None, _scanner_snapshot(scanner)))
    try:
        scanner.finish()
    except AssertionError:
        raise
    except Exception as error:
        observed.append(("finish", _exception_observable(error), _scanner_snapshot(scanner)))
        return observed
    observed.append(("finish", None, _scanner_snapshot(scanner)))
    return observed


def _assert_scanner_equivalent(
    pieces: list[str], case: str, guard: _RecordingSpecialSearch | None = None
) -> list[tuple[object, ...]]:
    """Require the production scanner to match the frozen original after every call."""
    candidate = _observe_scanner(_candidate_scanner_class(), pieces, guard)
    original = _observe_scanner(_OriginalJsonRootScanner, pieces)
    assert candidate == original, f"scanner observable diverged from original: {case}"
    return original


def test_original_scanner_oracle_is_pinned() -> None:
    """The frozen oracle is the pinned base copy and the production limits keep literals."""
    module_source = Path(__file__).read_text(encoding="utf-8")
    oracle_nodes = [
        node
        for node in ast.parse(module_source).body
        if isinstance(node, ast.ClassDef) and node.name == "_OriginalJsonRootScanner"
    ]
    assert len(oracle_nodes) == 1
    oracle_node = oracle_nodes[0]
    assert not oracle_node.decorator_list
    assert oracle_node.end_lineno is not None
    module_lines = module_source.split("\n")
    source = "\n".join(module_lines[oracle_node.lineno - 1 : oracle_node.end_lineno]) + "\n"
    assert hashlib.sha256(source.encode()).hexdigest() == _ORIGINAL_SCANNER_SOURCE_SHA256
    assert _ORACLE_STRING_CAPTURE_LIMIT == 64
    assert _ORACLE_JSON_NESTING_DEPTH == 64
    assert (
        frozenset({"item.started", "item.updated", "item.completed"}) == _ORACLE_OPAQUE_ITEM_TYPES
    )
    assert usage_codex._STRING_CAPTURE_LIMIT == 64  # pyright: ignore[reportPrivateUsage]
    assert usage_codex.MAX_JSON_NESTING_DEPTH == 64
    assert usage_codex.READ_CHUNK_BYTES == 65_536
    assert usage_codex.MAX_CODEX_OPAQUE_EVENT_BYTES == 8_388_608
    assert usage_codex.MAX_CODEX_OPAQUE_TOTAL_BYTES == 67_108_864
    assert (
        frozenset({"item.started", "item.updated", "item.completed"})
        == usage_codex.OPAQUE_ITEM_TYPES
    )
    assert (
        frozenset(
            {
                "thread.started",
                "turn.started",
                "item.started",
                "item.updated",
                "item.completed",
                "turn.failed",
                "turn.completed",
            }
        )
        == usage_codex.CODEX_EVENT_TYPES
    )


_EXHAUSTIVE_ALPHABET = (
    "a",
    '"',
    "\\",
    "u",
    "0",
    "F",
    "q",
    "n",
    "\x00",
    "\x1f",
    " ",
    "é",
    "\U0001d11e",
    "\x7f",
)
_EXHAUSTIVE_TEMPLATES = (
    ("value", _OPAQUE_STRING_PREFIX, '"}'),
    ("type", '{"type":"', '"}'),
    ("key", '{"', '":1}'),
    ("nested", '{"type":"item.updated","a":[["', '"]]}'),
)
_EXHAUSTIVE_RUN_LENGTHS = (62, 63, 64, 65)


def test_string_fast_path_matches_original_on_exhaustive_small_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """33,760 cases: 211 suffixes x 4 run lengths x 4 templates x 10 feeds.

    Every string of length at most 2 over a 14-symbol alphabet (211 suffixes)
    follows 62-65 ordinary characters, so the 64-character truncation edge falls
    at each suffix position. Feeds are one call, one character per call, and a
    two-piece split at each content index in [L-2, L+5]. The exhaustive claim
    covers only these short suffixes; it is not a proof over all inputs.
    """
    suffixes = [
        "",
        *_EXHAUSTIVE_ALPHABET,
        *("".join(pair) for pair in itertools.product(_EXHAUSTIVE_ALPHABET, repeat=2)),
    ]
    assert len(suffixes) == 211
    guard = _install_search_guard(monkeypatch)
    cases = 0
    for (name, prefix, closing), length, suffix in itertools.product(
        _EXHAUSTIVE_TEMPLATES, _EXHAUSTIVE_RUN_LENGTHS, suffixes
    ):
        text = prefix + ("x" * length) + suffix + closing
        feeds = [[text], list(text)]
        for index in range(length - 2, length + 6):
            split = len(prefix) + index
            feeds.append([text[:split], text[split:]])
        for feed_index, pieces in enumerate(feeds):
            _assert_scanner_equivalent(
                pieces, f"exhaustive {name} L={length} suffix={suffix!r} feed={feed_index}", guard
            )
            cases += 1
    assert cases == 33_760


_SEEDED_VALID_ESCAPES = ('\\"', "\\\\", "\\/", "\\b", "\\f", "\\n", "\\r", "\\t")
_SEEDED_INVALID_ESCAPES = ("\\x", "\\U", "\\u12G4", '\\u1"')
_SEEDED_MULTIBYTE = ("é", "ß", "€", "中", "\U0001d11e", "\U0001f600")
_SEEDED_ORDINARY = "abcxyzABC 0189~/'"
_SEEDED_PUNCTUATION = '{}[],:"'


def _seeded_string_body(rng: random.Random) -> str:
    """Return one 0-300 character JSON string body from a weighted token mix."""
    target = rng.randint(0, 300)
    parts: list[str] = []
    if rng.random() < 0.7:
        parts.append("x" * rng.randint(55, 75))
    while sum(len(part) for part in parts) < target:
        roll = rng.random()
        if roll < 0.45:
            parts.append(rng.choice(_SEEDED_ORDINARY) * rng.randint(1, 40))
        elif roll < 0.55:
            parts.append(chr(rng.randrange(32)))
        elif roll < 0.60:
            parts.append('"')
        elif roll < 0.64:
            parts.append("\\")
        elif roll < 0.75:
            parts.append(rng.choice(_SEEDED_VALID_ESCAPES))
        elif roll < 0.83:
            digits = "".join(rng.choice("0123456789abcdefABCDEF") for _ in range(4))
            parts.append("\\u" + digits)
        elif roll < 0.88:
            parts.append(rng.choice(_SEEDED_INVALID_ESCAPES))
        else:
            parts.append(rng.choice(_SEEDED_MULTIBYTE))
    return "".join(parts)[:target]


def _seeded_event(rng: random.Random) -> list[str]:
    """Return one generated event, optionally structurally mutated, as random pieces."""
    _name, prefix, closing = rng.choice(_EXHAUSTIVE_TEMPLATES)
    text = prefix + _seeded_string_body(rng) + closing
    mutation = rng.random()
    if mutation < 0.08:
        text = text[: rng.randint(0, len(text))]
    elif mutation < 0.16:
        position = rng.randint(0, len(text))
        text = text[:position] + rng.choice(_SEEDED_PUNCTUATION) + text[position:]
    elif mutation < 0.24 and text:
        position = rng.randrange(len(text))
        text = text[:position] + text[position + 1 :]
    cuts = sorted(rng.randint(0, len(text)) for _ in range(rng.randint(0, 4)))
    bounds = [0, *cuts, len(text)]
    return [text[start:end] for start, end in itertools.pairwise(bounds)]


def test_string_fast_path_matches_original_on_seeded_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2,500 seeded events (1,250 each from seeds 1026 and 6201) match the original.

    The weighted generator covers all 32 C0 controls, quotes, lone backslashes,
    all eight valid escapes, mixed-case ``\\uXXXX``, invalid escapes, 2/3/4-byte
    characters, lengths 0-300, structural mutations, and random call splits.
    """
    guard = _install_search_guard(monkeypatch)
    accepted = rejected = truncated = 0
    for seed in (1026, 6201):
        rng = random.Random(seed)
        for case in range(1_250):
            pieces = _seeded_event(rng)
            original = _assert_scanner_equivalent(pieces, f"seeded {seed}:{case} {pieces!r}", guard)
            if original[-1][0] == "finish" and original[-1][1] is None:
                accepted += 1
            else:
                rejected += 1
            truncated += any(cast(tuple[object, ...], step[2])[4] for step in original)
    assert accepted + rejected == 2_500
    assert accepted >= 600
    assert rejected >= 1_500
    assert truncated >= 1_000


class _PlannedChunkReader:
    """Return byte chunks whose sizes follow a deterministic plan."""

    def __init__(self, content: bytes, sizes: Iterator[int]) -> None:
        """Initialize the content and the chunk-size plan."""
        self._content = content
        self._sizes = sizes
        self._offset = 0

    def read(self, size: int) -> bytes:
        """Return at most ``size`` bytes, capped by the next planned chunk size."""
        assert size <= READ_CHUNK_BYTES
        count = min(size, next(self._sizes))
        result = self._content[self._offset : self._offset + count]
        self._offset += len(result)
        return result


def _random_chunk_sizes(seed: int, largest: int) -> Iterator[int]:
    """Yield an endless deterministic sequence of chunk sizes in [1, largest]."""
    rng = random.Random(seed)
    while True:
        yield rng.randint(1, largest)


_STREAM_CHUNK_PLANS: tuple[tuple[str, Callable[[], Iterator[int]]], ...] = (
    ("1", lambda: itertools.repeat(1)),
    ("2", lambda: itertools.repeat(2)),
    ("3", lambda: itertools.repeat(3)),
    ("7", lambda: itertools.repeat(7)),
    ("65536", lambda: itertools.repeat(65_536)),
    ("random-65536", lambda: _random_chunk_sizes(1026, 65_536)),
    ("random-97", lambda: _random_chunk_sizes(1027, 97)),
)
_STREAM_MULTIBYTE = ("é€\U0001d11e" * 3).encode()


def _sized_stream_event(prefix: bytes, size: int) -> bytes:
    """Return one event of exactly ``size`` bytes with a truncated multibyte payload."""
    suffix = b'"}\n'
    fill = size - len(prefix) - len(suffix) - 70 - len(_STREAM_MULTIBYTE)
    assert fill >= 0
    event = prefix + (b"x" * 70) + _STREAM_MULTIBYTE + (b"y" * fill) + suffix
    assert len(event) == size
    return event


def _stream_item(size: int) -> bytes:
    """Return one opaque item event of exactly ``size`` bytes."""
    return _sized_stream_event(_OPAQUE_STRING_PREFIX.encode(), size)


def _stream_retained(size: int) -> bytes:
    """Return one retained ``turn.started`` event of exactly ``size`` bytes."""
    return _sized_stream_event(b'{"type":"turn.started","payload":"', size)


def _stream_cases() -> list[tuple[str, bytes, str | None]]:
    """Return stream inputs with literal expected outcomes under the reduced limits."""
    terminal = _codex_terminal_event()
    truncated = _OPAQUE_STRING_PREFIX.encode() + (b"x" * 70)
    return [
        ("valid-multibyte", _stream_item(600) + terminal, None),
        ("utf8-mid-string", truncated + b'\xffx"}\n' + terminal, "invalid UTF-8"),
        ("utf8-surrogate", truncated + b'\xed\xa0\x80"}\n' + terminal, "invalid UTF-8"),
        ("utf8-final-flush", terminal + truncated + b"\xe2\x82", "invalid UTF-8"),
        ("partial-event", terminal + truncated, "partial event"),
        ("c0-after-truncation", truncated + b'\x01"}\n' + terminal, "malformed Codex JSON"),
        ("escapes-after-truncation", truncated + b'\\u00e9\\n\\""}\n' + terminal, None),
        (
            "type-first-retained-oversize",
            b'{"type":"turn.started","payload":"' + (b"x" * 400) + b'"}\n' + terminal,
            "retained event exceeds size limit",
        ),
        (
            "late-discriminator-opaque",
            b'{"payload":"'
            + (b"x" * 500)
            + _STREAM_MULTIBYTE
            + b'","type":"item.updated"}\n'
            + terminal,
            None,
        ),
        ("opaque-event-exact", _stream_item(1_024) + terminal, None),
        ("opaque-event-one-over", _stream_item(1_025) + terminal, "opaque event exceeds"),
        ("opaque-total-exact", _stream_item(1_000) * 3 + terminal, None),
        (
            "opaque-total-one-over",
            _stream_item(1_000) * 2 + _stream_item(1_001) + terminal,
            "opaque stream exceeds total byte limit",
        ),
        ("event-count-exact", _stream_item(140) * 4 + terminal, None),
        ("event-count-one-over", _stream_item(140) * 5 + terminal, "event count limit"),
        ("retained-exact", _stream_retained(256) + terminal, None),
        ("retained-one-over", _stream_retained(257) + terminal, "retained event exceeds"),
        ("opaque-item-at-retained-bound", _stream_item(256) + terminal, None),
        ("opaque-item-over-retained-bound", _stream_item(257) + terminal, None),
    ]


def _observe_stream(content: bytes, sizes: Iterator[int]) -> tuple[object, ...]:
    """Parse one planned-chunk stream into its usage dump or its error observable."""
    try:
        return ("ok", parse_codex_stream(_PlannedChunkReader(content, sizes)).model_dump())
    except AssertionError:
        raise
    except Exception as error:
        return ("error", _exception_observable(error))


def test_string_fast_path_stream_outcomes_match_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """19 stream inputs x 7 byte-chunk plans match the original scanner end to end.

    Reduced limits (256-byte retained event, 1,024-byte opaque event, 3,000-byte
    opaque total, five events) put every counter edge within a few kilobytes.
    """
    monkeypatch.setattr(usage_codex, "MAX_EVENT_BYTES", 256)
    monkeypatch.setattr(usage_codex, "MAX_CODEX_OPAQUE_EVENT_BYTES", 1_024)
    monkeypatch.setattr(usage_codex, "MAX_CODEX_OPAQUE_TOTAL_BYTES", 3_000)
    monkeypatch.setattr(usage_codex, "MAX_EVENT_COUNT", 5)
    cases = _stream_cases()
    assert len(cases) == 19
    for name, content, expected in cases:
        for plan_name, plan in _STREAM_CHUNK_PLANS:
            candidate = _observe_stream(content, plan())
            with monkeypatch.context() as patch:
                patch.setattr(usage_codex, "_JsonRootScanner", _OriginalJsonRootScanner)
                original = _observe_stream(content, plan())
            case = f"stream {name} chunks={plan_name}"
            assert candidate == original, f"stream observable diverged from original: {case}"
            if expected is None:
                assert candidate[0] == "ok", case
            else:
                error = cast(tuple[object, ...], candidate[1])
                assert expected in cast(str, error[1]), case


_TRUNCATED_ANCHOR_PREFIX = _OPAQUE_STRING_PREFIX + ("x" * 65)
_TRUNCATED_ANCHOR_CASES = [
    *(
        pytest.param(_TRUNCATED_ANCHOR_PREFIX + chr(code) + '"}', False, id=f"c0-{code:02x}")
        for code in range(32)
    ),
    *(
        pytest.param(_TRUNCATED_ANCHOR_PREFIX + escape + '"}', True, id=f"escape-{index}")
        for index, escape in enumerate(_SEEDED_VALID_ESCAPES)
    ),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\u00e9"}', True, id="unicode-00e9"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\uD834"}', True, id="unicode-d834"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\q"}', False, id="invalid-escape-q"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\U"}', False, id="invalid-escape-U"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\u12G4"}', False, id="invalid-unicode-12G4"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\\q\\"}', False, id="m2-anchor"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\x7f"}', True, id="del-007f"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + 'é"}', True, id="two-byte-00e9"),
    pytest.param(_TRUNCATED_ANCHOR_PREFIX + '\U0001d11e"}', True, id="four-byte-1d11e"),
]


_MALFORMED_SCANNER_ERROR = (
    CodexUsageParseError,
    "malformed Codex JSON event",
    ("malformed Codex JSON event",),
    True,
    True,
    False,
)


def _scan_pieces(pieces: list[str], finish: bool = True) -> tuple[tuple[object, ...] | None, Any]:
    """Run the production scanner, returning its error observable (or None) and itself."""
    scanner = _candidate_scanner_class()()
    try:
        for piece in pieces:
            scanner.consume(piece)
        if finish:
            scanner.finish()
    except AssertionError:
        raise
    except Exception as raised:
        return _exception_observable(raised), scanner
    return None, scanner


def _stream_outcome(content: bytes) -> tuple[object, ...]:
    """Parse one stream in full-size reads into its usage dump or error observable."""
    return _observe_stream(content, itertools.repeat(READ_CHUNK_BYTES))


def _assert_stream_usage(content: bytes, case: str) -> None:
    """Require one stream to parse to the synthetic terminal usage."""
    outcome = _stream_outcome(content)
    assert outcome[0] == "ok", f"stream rejected: {case}: {outcome!r}"
    assert cast(dict[str, object], outcome[1])["input_tokens"] == 1, case


@pytest.mark.parametrize(("text", "accepted"), _TRUNCATED_ANCHOR_CASES)
def test_truncated_string_literal_anchors(text: str, accepted: bool) -> None:
    """Literal, oracle-independent outcomes for special characters after truncation."""
    for feed_name, pieces in (("whole", [text]), ("per-character", list(text))):
        error, scanner = _scan_pieces(pieces)
        if accepted:
            assert error is None, f"accepted anchor rejected: feed={feed_name}"
            assert scanner.is_opaque_item, feed_name
            assert vars(scanner)["_string"] == ["x"] * 64, feed_name
        else:
            assert error == _MALFORMED_SCANNER_ERROR, f"rejected anchor outcome: feed={feed_name}"
    line = text.encode() + b"\n"
    if accepted:
        _assert_stream_usage(line + _codex_terminal_event(), "accepted anchor stream")
    else:
        assert _stream_outcome(line) == ("error", _MALFORMED_SCANNER_ERROR)
    if text.endswith('\\q\\"}'):
        assert line == b'{"type":"item.updated","payload":"' + b"x" * 65 + b'\\q\\"}\n'


def test_prefix_and_discriminator_boundaries() -> None:
    """Literal discriminator capture at 63, 64, and 65 characters, scanner and stream."""
    terminal = _codex_terminal_event()
    for length in (63, 64, 65):
        value = "a" * length
        expected = value if length <= 64 else None
        error, scanner = _scan_pieces(['{"type":"' + value + '"}'])
        assert error is None, f"type value {length} rejected"
        assert vars(scanner)["_root_type_value"] == expected, f"type value {length}"
        assert vars(scanner)["_root_type_count"] == 1
        assert not scanner.is_opaque_item
        assert scanner.has_nonopaque_root_type
        outcome = _stream_outcome(('{"type":"' + value + '"}\n').encode())
        assert outcome[0] == "error", f"type value {length} stream accepted"
        assert cast(tuple[object, ...], outcome[1])[1] == "unknown Codex event type"

        key = "k" * length
        error, scanner = _scan_pieces(['{"' + key + '"'], finish=False)
        assert error is None, f"key {length} rejected"
        assert vars(scanner)["_stack"] == [("object", "colon", key if length <= 64 else None)]
        error, scanner = _scan_pieces(['{"' + key + '"', ':1,"type":"item.updated"}'])
        assert error is None, f"key {length} event rejected"
        assert scanner.is_opaque_item
        assert not scanner.has_nonopaque_root_type
        event = ('{"' + key + '":1,"type":"item.updated"}\n').encode()
        _assert_stream_usage(event + terminal, f"key {length} stream")

        payload = "x" * (length + 1)
        error, scanner = _scan_pieces(['{"payload":"' + payload + '","type":"item.updated"}'])
        assert error is None, f"late discriminator after {length + 1} rejected"
        assert vars(scanner)["_root_type_value"] == "item.updated"
        assert scanner.is_opaque_item
        assert not scanner.has_nonopaque_root_type
        event = ('{"payload":"' + payload + '","type":"item.updated"}\n').encode()
        _assert_stream_usage(event + terminal, f"late discriminator after {length + 1}")


def _longest_retained_sequence(value: object) -> int:
    """Return the longest string, list, or tuple length reachable inside one field."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        return max([len(items), *(_longest_retained_sequence(item) for item in items)])
    return 0


def test_scanner_retains_no_opaque_content() -> None:
    """1 MiB unclosed and closed truncated strings keep only base fields and a 64-char prefix."""
    error, scanner = _scan_pieces([_OPAQUE_STRING_PREFIX + ("x" * 1_048_576)], finish=False)
    assert error is None
    state = cast(dict[str, object], vars(scanner))
    assert set(state) == {
        "_stack",
        "_root_started",
        "_root_finished",
        "_string",
        "_string_truncated",
        "_string_state",
        "_unicode_digits",
        "_primitive_kind",
        "_primitive_state",
        "_root_type_count",
        "_root_type_value",
    }
    assert state["_string_state"] == "string"
    assert state["_string_truncated"] is True
    assert _longest_retained_sequence(state["_string"]) == 64
    assert max(_longest_retained_sequence(value) for value in state.values()) <= 64

    # A closed long string takes the fast path's found-special branch, so skipped
    # text must not be retained there either (appended or in a new attribute).
    closed = _OPAQUE_STRING_PREFIX + ("x" * 1_048_576) + '"}'
    error, scanner = _scan_pieces([closed])
    assert error is None
    state = cast(dict[str, object], vars(scanner))
    assert set(state) == {
        "_stack",
        "_root_started",
        "_root_finished",
        "_string",
        "_string_truncated",
        "_string_state",
        "_unicode_digits",
        "_primitive_kind",
        "_primitive_state",
        "_root_type_count",
        "_root_type_value",
    }, f"closed string left extra fields: {sorted(state)}"
    assert state["_string_state"] == "none"
    assert state["_string_truncated"] is True
    assert state["_string"] == ["x"] * 64, "closed string retained more than its prefix"
    assert max(_longest_retained_sequence(value) for value in state.values()) <= 64
    assert scanner.is_opaque_item


def _escape_split_feeds() -> list[tuple[str, list[str]]]:
    """Return truncated-string escapes split at every internal boundary and per character."""
    prefix = _OPAQUE_STRING_PREFIX + ("x" * 70)
    feeds: list[tuple[str, list[str]]] = []
    for escape in ("\\uABCD", "\\n"):
        for split in range(1, len(escape)):
            feeds.append((escape, [prefix + escape[:split], escape[split:] + 'xyz"}']))
        feeds.append((escape, [prefix, *escape, 'xyz"}']))
    return feeds


def test_escape_and_unicode_states_never_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Split escapes after truncation reach the authentic per-character path in full."""
    scanner_class = _candidate_scanner_class()
    original_consume = scanner_class.__dict__["_consume_string"]
    seen: list[str] = []

    def recording_consume(self: Any, character: str) -> None:
        """Record each character before delegating to the authentic string automaton."""
        seen.append(character)
        original_consume(self, character)

    monkeypatch.setattr(scanner_class, "_consume_string", recording_consume)
    guard = _install_search_guard(monkeypatch)
    for escape, pieces in _escape_split_feeds():
        seen.clear()
        original = _assert_scanner_equivalent(pieces, f"escape split {pieces[-2:]!r}", guard)
        assert original[-1][:2] == ("finish", None)
        assert escape in "".join(seen), f"escape skipped validation: {pieces[-2:]!r}"


def _work_witness_feeds(length: int) -> list[tuple[str, list[str]]]:
    """Return the whole, 65,536-character, and per-character feeds of one witness event."""
    text = _OPAQUE_STRING_PREFIX + ("x" * length) + '"}'
    chunks = [text[start : start + 65_536] for start in range(0, len(text), 65_536)]
    return [("whole", [text]), ("65536", chunks), ("per-character", list(text))]


def _work_witness_final_state(length: int) -> tuple[object, ...]:
    """Return the literal final scanner observable for one accepted witness event."""
    return (
        (),
        True,
        True,
        ("x",) * min(length, 64),
        length > 64,
        "none",
        (),
        None,
        "",
        1,
        "item.updated",
        True,
        False,
        True,
    )


def _counted_scan(
    monkeypatch: pytest.MonkeyPatch,
    scanner_class: type[Any],
    pieces: list[str],
    guard: _RecordingSpecialSearch | None = None,
) -> tuple[int, tuple[object, ...] | None, tuple[object, ...]]:
    """Count ``_consume_string`` calls for one feed, returning error and final state."""
    calls = 0
    original_consume = scanner_class.__dict__["_consume_string"]

    def counting_consume(self: Any, character: str) -> None:
        """Count one authentic per-character string-automaton step."""
        nonlocal calls
        calls += 1
        original_consume(self, character)

    scanner = scanner_class()
    error: tuple[object, ...] | None = None
    with monkeypatch.context() as patch:
        patch.setattr(scanner_class, "_consume_string", counting_consume)
        try:
            for piece in pieces:
                if guard is not None:
                    guard.begin(len(piece))
                scanner.consume(piece)
            scanner.finish()
        except AssertionError:
            raise
        except Exception as raised:
            error = _exception_observable(raised)
    return calls, error, _scanner_snapshot(scanner)


def test_string_fast_path_work_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic work: 26 + min(N, 65) + 1 string steps, versus N + 27 originally.

    The ``type`` key (5), ``item.updated`` value (13), and ``payload`` key (8) cost
    26 steps; the payload costs min(N, 65) characters plus its closing quote. The
    65th payload character sets truncation, after which ordinary characters skip.
    """
    expected_candidate = {64: 91, 65: 92, 66: 92, 1_048_576: 92}
    expected_original = {64: 91, 65: 92, 66: 93, 1_048_576: 1_048_603}
    for length, feeds in ((length, _work_witness_feeds(length)) for length in expected_candidate):
        for feed_name, pieces in feeds:
            calls, error, state = _counted_scan(monkeypatch, _candidate_scanner_class(), pieces)
            case = f"N={length} feed={feed_name}"
            assert (calls, error) == (expected_candidate[length], None), case
            assert state == _work_witness_final_state(length), case
        original_calls, original_error, original_state = _counted_scan(
            monkeypatch, _OriginalJsonRootScanner, feeds[0][1]
        )
        assert (original_calls, original_error) == (expected_original[length], None)
        assert original_state == _work_witness_final_state(length)


def test_string_fast_path_search_is_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Searches never rescan and are bounded by consume calls plus truncated specials.

    Covers every work-witness feed and every split-escape feed. Each long ordinary
    run must be found by at least one search that skipped characters, so a fast
    path that is never entered fails here as well as in the work witness.
    """
    guard = _install_search_guard(monkeypatch)
    scanner_class = _candidate_scanner_class()
    original_consume = scanner_class.__dict__["_consume_string"]
    truncated_specials = 0

    def special_counting_consume(self: Any, character: str) -> None:
        """Count special characters that reach the automaton inside a truncated string."""
        nonlocal truncated_specials
        state = cast(dict[str, object], vars(self))
        if (
            state["_string_state"] == "string"
            and state["_string_truncated"] is True
            and (character in '"\\' or ord(character) < 0x20)
        ):
            truncated_specials += 1
        original_consume(self, character)

    monkeypatch.setattr(scanner_class, "_consume_string", special_counting_consume)
    inputs: list[tuple[str, list[str], int]] = []
    for length in (64, 65, 66, 1_048_576):
        for feed_name, pieces in _work_witness_feeds(length):
            inputs.append((f"witness N={length} feed={feed_name}", pieces, length))
    for escape, pieces in _escape_split_feeds():
        inputs.append((f"escape {escape!r} split {pieces[-2:]!r}", pieces, -1))
    for case, pieces, length in inputs:
        guard.calls = guard.skipped_runs = guard.total_spent = 0
        truncated_specials = 0
        if length >= 0:
            scanner = scanner_class()
            error: tuple[object, ...] | None = None
            try:
                for piece in pieces:
                    guard.begin(len(piece))
                    scanner.consume(piece)
                scanner.finish()
            except AssertionError:
                raise
            except Exception as raised:
                error = _exception_observable(raised)
            assert error is None, case
            assert _scanner_snapshot(scanner) == _work_witness_final_state(length), case
        else:
            candidate = _observe_scanner(scanner_class, pieces, guard)
            assert candidate == _observe_scanner(_OriginalJsonRootScanner, pieces), case
        assert guard.total_spent <= sum(len(piece) for piece in pieces), case
        assert guard.calls <= len(pieces) + truncated_specials, case
        if length > 64 or length < 0:
            assert guard.calls >= 1, f"fast path never searched: {case}"
        else:
            assert guard.calls == 0, f"untruncated string searched: {case}"
        if length > 65 or length < 0:
            # Only these inputs hold ordinary characters after truncation is set.
            assert guard.skipped_runs >= 1, f"fast path never skipped a run: {case}"


@pytest.mark.parametrize("removed_key", ["speed", "input_tokens"])
def test_claude_terminal_usage_missing_installed_key_fails_closed(removed_key: str) -> None:
    """Every installed terminal usage key is required even when not retained."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    del terminal["usage"][removed_key]
    with pytest.raises(ClaudeUsageParseError, match="usage schema"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


def test_claude_terminal_usage_extra_or_model_usage_drift_fails_closed() -> None:
    """Unknown terminal or per-model schema keys cannot be silently accepted."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="usage schema"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


def test_claude_duplicate_json_keys_fail_closed() -> None:
    """Duplicate result or model-usage keys cannot select a retained value silently."""
    duplicate_result = b'{"type":"result","type":"result"}\n'
    duplicate_model = (
        b'{"type":"result","subtype":"success","is_error":false,"usage":{},'
        b'"modelUsage":{"model":{"inputTokens":1,"inputTokens":2}}}\n'
    )
    for stream in (duplicate_result, duplicate_model):
        with pytest.raises(ClaudeUsageParseError, match="duplicate JSON keys"):
            parse_claude_stream(
                BytesIO(stream), expected_version="2.1.233", require_launch_authority=False
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [("subtype", "error"), ("subtype", "unknown"), ("is_error", True), ("is_error", "false")],
)
def test_claude_terminal_requires_observed_success_status(field: str, value: object) -> None:
    """Only the observed success/false result status may supply terminal usage."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal[field] = value
    with pytest.raises(ClaudeUsageParseError, match="status is invalid"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


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
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


def test_claude_malformed_top_level_usage_and_non_finite_model_sum_fail_closed() -> None:
    """Top-level fields remain validated even though modelUsage alone supplies totals."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["input_tokens"] = "invalid"
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude usage field"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    for model in terminal["modelUsage"].values():
        model["costUSD"] = 1e308
    with pytest.raises(ClaudeUsageParseError, match="modelUsage costUSD sum"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["modelUsage"]["synthetic-primary"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="modelUsage schema"):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


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
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"),
            expected_version="2.1.233",
            require_launch_authority=False,
        )


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
    version_calls = 0

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
        nonlocal version_calls
        if argv[-1] == "--version":
            observed["version_argv"] = argv
            observed["version_kwargs"] = kwargs
            version_calls += 1
            return FakeProcess(b"codex-cli 0.148.0\n")
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        process = FakeProcess(_codex_terminal_event())
        observed["process"] = process
        return process

    def fake_which(_: str) -> str:
        return "/stub/codex"

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)
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
    assert observed["argv"] == [
        "/stub/CODEX",
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
    assert observed["version_argv"] == ["/stub/CODEX", "--version"]
    assert version_calls == 1
    record = (tmp_path / ".agent-usage/usage.jsonl").read_text()
    assert sentinel.decode() not in record
    assert "SENTINEL_PROMPT_CONTENT" not in str(observed["argv"])
    assert json.loads(record)["effort"] == "high"
    parsed_record = json.loads(record)
    assert parsed_record["harness_version"] == "0.148.0"
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


def _process_finished_or_zombie(
    pid: int, *, stat_reader: Callable[[int], str] | None = None
) -> bool:
    """Whether a timeout-cleanup process has exited or is a harmless zombie.

    Args:
        pid: The process identifier to probe.
        stat_reader: Injectable ``/proc/<pid>/stat`` reader for deterministic
            exception-grammar tests.

    Returns:
        ``True`` when the process no longer exists or has become a zombie.

    Raises:
        PermissionError: If the stat read is not permitted.
        OSError: If the stat read fails for a reason other than a missing process.
        IndexError: If procfs returns malformed or short stat data.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    read_stat: Callable[[int], str] = stat_reader or _read_proc_stat
    try:
        state = read_stat(pid).split()[2]
    except (ProcessLookupError, FileNotFoundError):
        return True
    return state == "Z"


def _read_proc_stat(pid: int) -> str:
    """Read one process's procfs stat record for the timeout-cleanup assertion."""
    return Path(f"/proc/{pid}/stat").read_text()


def test_timeout_cleanup_liveness_helper_handles_expected_exit_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow liveness grammar accepts only raced-away and zombie processes."""
    calls = 0

    def unreadable_stat(_: int) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("stat reader must not run after kill ESRCH")

    def missing_kill(_: int, __: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", missing_kill)
    assert _process_finished_or_zombie(123, stat_reader=unreadable_stat) is True
    assert calls == 0

    def reachable_kill(_: int, __: int) -> None:
        return None

    monkeypatch.setattr(os, "kill", reachable_kill)
    assert (
        _process_finished_or_zombie(
            123, stat_reader=lambda _: (_ for _ in ()).throw(ProcessLookupError())
        )
        is True
    )
    assert (
        _process_finished_or_zombie(
            123, stat_reader=lambda _: (_ for _ in ()).throw(FileNotFoundError())
        )
        is True
    )
    assert _process_finished_or_zombie(123, stat_reader=lambda _: "123 (stub) Z") is True
    assert _process_finished_or_zombie(123, stat_reader=lambda _: "123 (stub) S") is False


@pytest.mark.parametrize(
    "error",
    [PermissionError(), OSError(errno.EIO, "unrelated stat failure")],
)
def test_timeout_cleanup_liveness_helper_propagates_unexpected_stat_errors(
    error: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission and unrelated procfs failures cannot be mistaken for cleanup success."""

    def reachable_kill(_: int, __: int) -> None:
        return None

    monkeypatch.setattr(os, "kill", reachable_kill)

    def fail_stat(_: int) -> str:
        raise error

    with pytest.raises(type(error)):
        _process_finished_or_zombie(123, stat_reader=fail_stat)


@pytest.mark.parametrize(
    "error",
    [PermissionError(), OSError(errno.EIO, "unrelated kill probe failure")],
)
def test_timeout_cleanup_liveness_helper_propagates_unexpected_kill_errors(
    error: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission and unrelated kill-probe failures cannot imply cleanup success."""
    stat_calls = 0

    def fail_kill(_: int, __: int) -> None:
        raise error

    def unreadable_stat(_: int) -> str:
        nonlocal stat_calls
        stat_calls += 1
        raise AssertionError("stat reader must not run after a kill-probe failure")

    monkeypatch.setattr(os, "kill", fail_kill)

    with pytest.raises(type(error)):
        _process_finished_or_zombie(123, stat_reader=unreadable_stat)
    assert stat_calls == 0


@pytest.mark.serial(reason="drives a real TERM-ignoring process group")
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
        for _ in range(300):
            if marker.exists():
                return real_start_deadline(process, seconds, timed_out)
            time.sleep(0.01)
        pytest.fail("fixed Git stub did not establish the timeout readiness handshake")

    monkeypatch.setattr(usage_cli, "_start_deadline", start_after_stub_ready)

    with pytest.raises(CaptureUsageError, match="Git worktree metadata is unavailable"):
        usage_cli._git_output(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]

    pid = int(marker.read_text())
    for _ in range(20):
        if _process_finished_or_zombie(pid):
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
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)
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


@pytest.mark.serial(reason="drives a real stdout-inheriting process group")
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
        if _process_finished_or_zombie(descendant):
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


def test_prompt_cap_accepts_exactly_64_kib_and_rejects_one_byte_more(
    tmp_path: Path,
) -> None:
    """The bounded review brief fits, while the next byte still fails closed."""
    assert usage_cli.MAX_PROMPT_BYTES == 65_536
    exact = tmp_path / "exact-prompt"
    exact.write_bytes(b"x" * usage_cli.MAX_PROMPT_BYTES)
    assert len(usage_cli._prompt_bytes(exact)) == usage_cli.MAX_PROMPT_BYTES  # pyright: ignore[reportPrivateUsage]

    oversized = tmp_path / "oversized-prompt"
    oversized.write_bytes(b"x" * (usage_cli.MAX_PROMPT_BYTES + 1))
    with pytest.raises(CaptureUsageError, match="prompt file cannot be safely opened"):
        usage_cli._prompt_bytes(oversized)  # pyright: ignore[reportPrivateUsage]


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
        *NATIVE_ROLE_EXCLUSIONS,
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
        expected_version="2.1.233",
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
        expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
        expected_version="2.1.233",
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


def _rebind_rows_to_live_pin(rows: list[dict[str, Any]], role_value: str) -> None:
    """Rebind recorded ``message.model``/``effort`` rows to the role's committed pin.

    Roles outside the native roster keep their recorded values unchanged.
    """
    if role_value not in {role.value for role in NativeClaudeRole}:
        return
    pin = usage_cli._native_role_pin(NativeClaudeRole(role_value))  # pyright: ignore[reportPrivateUsage]
    for row in rows:
        message = row.get("message")
        if isinstance(message, dict) and "model" in message:
            cast(dict[str, Any], message)["model"] = pin.model
        if "effort" in row:
            row["effort"] = pin.effort


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
    _rebind_rows_to_live_pin(rows, role_value)
    rows[-1]["message"]["content"] = [{"type": "text", "text": text}]
    return _dump_rows(rows)


_PROVEN_STORY_PLANNER_VERSION = "2.1.310"
"""The one Claude Code version D219 admits for a native ``story-planner`` launch."""
_PROVEN_STORY_PLANNER_VERSION_OUTPUT = b"2.1.310 (Claude Code)\n"
"""The exact raw ``claude --version`` release spelling observed on 2.1.310."""


def _admit_proven_story_planner_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the stubbed Claude version probe report the D219 proven version.

    The autouse launcher isolation stubs every Claude probe to ``2.1.233``,
    which D219's G2 now refuses for ``story-planner`` before launch.
    """

    def proven_version(executable: str) -> tuple[str, bytes]:
        assert Path(executable).name.lower() == "claude"
        return _PROVEN_STORY_PLANNER_VERSION, _PROVEN_STORY_PLANNER_VERSION_OUTPUT

    monkeypatch.setattr(usage_cli, "_harness_version_probe", proven_version)


def _proven_story_planner_transcript_bytes(
    *, session_id: str = _NATIVE_SESSION_ID, text: str = "SYNTHETIC_STORY_HANDOFF"
) -> bytes:
    """Return the committed ``story-planner`` fixture bound to the D219 proven version.

    D219's G2 refuses any unproven version before launch, so a fake launch must
    probe ``2.1.310``; the parser requires every versioned row to equal the
    probe, so only the ``version`` field is rebound, like the session id. The
    row grammar stays the committed 2.1.233 fixture's; real 2.1.310 grammar is
    the parent's live-run proof, not this hardware-free substitute.
    """
    rows: list[dict[str, Any]] = [
        json.loads(line)
        for line in _read_only_transcript_bytes(
            "story-planner", session_id=session_id, text=text
        ).splitlines()
    ]
    for row in rows:
        if "version" in row:
            row["version"] = _PROVEN_STORY_PLANNER_VERSION
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
    _rebind_rows_to_live_pin(rows, target_role)
    rows[-1]["message"]["content"] = [{"type": "text", "text": text}]
    return _dump_rows(rows)


def test_native_read_only_emits_bounded_handback_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful READ_ONLY run emits exactly one framed, integrity-checkable stdout line."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    _stub_plan_root(monkeypatch)
    processes: list[_NativeProcess] = []
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(text="SYNTHETIC_STORY_HANDOFF"),
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(),
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(),
        ),
    )

    def failing_append(_path: Path, _record: object, *, root_descriptor: int | None = None) -> None:
        del root_descriptor
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(text=sentinel),
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(text=adversarial),
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(text="0123456789"),
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
            expected_version="2.1.233",
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
            expected_version="2.1.233",
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
    """The closed 12-key map binds exactly under the root; other roles see none of it."""
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
        "COFFEE_ROAST_LOG_DIR": os.path.join(resolved_root, "tmp", "coffee-roast-logs"),
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

    worker_argv = observed[0][0]
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

    worker_argv = observed[0][0]
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
    worker_argv = observed[0][0]
    assert "--add-dir" not in worker_argv


def test_validation_environment_key_set_has_exactly_twelve_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed key set is exactly twelve names; a WRITE launch strips every one."""
    keys = usage_cli._VALIDATION_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    assert len(keys) == 12
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


@pytest.mark.docs
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
        args = [
            "print-validation-commands",
            "--role",
            role.value,
            "--validation-root",
            str(root),
        ]
        tokens: tuple[str, ...] = ()
        if role is NativeClaudeRole.QA:
            tokens = ("tests/", "--cov=src", "--cov-branch", "--cov-report=term-missing")
            for token in tokens:
                args.append(f"--pytest-arg={token}")
        exit_code = main(args)
        assert exit_code == 0
        printed = capsys.readouterr().out.splitlines()
        expected_rendered = render_validation_commands(role, resolved_root)
        allow_lines = [
            f"ALLOW {command.kind.value} {text}"
            for command, text in zip(VALIDATION_ROLE_COMMANDS[role], expected_rendered, strict=True)
        ]
        run_lines = [
            f"RUN {usage_cli._render_prefix_run_command(text, tokens)}"  # pyright: ignore[reportPrivateUsage]
            if command.kind is ValidationCommandKind.PREFIX
            else f"RUN {text}"
            for command, text in zip(VALIDATION_ROLE_COMMANDS[role], expected_rendered, strict=True)
        ]
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
                "--pytest-arg",
                "tests/",
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
                "--pytest-arg=tests/",
                "--pytest-arg=--cov=src",
                "--pytest-arg=--cov-branch",
                "--pytest-arg=--cov-report=term-missing",
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


def test_safety_and_security_reviewers_use_ordinary_role_path() -> None:
    """Option B excludes whole-diff assurance roles from native capture."""
    agents = Path(__file__).resolve().parents[1] / ".claude" / "agents"
    native_values = {role.value for role in NativeClaudeRole}
    for name in ("safety-reviewer", "security-reviewer"):
        text = (agents / f"{name}.md").read_text()
        assert name not in native_values
        assert name in NATIVE_ROLE_EXCLUSIONS
        assert "tools: Read, Grep, Glob, Bash" in text
    assert (
        "python -m pytest tests/test_controller.py tests/test_safety.py -q"
        in (agents / "safety-reviewer.md").read_text()
    )
    assert "git diff origin/main...HEAD" in (agents / "security-reviewer.md").read_text()


def test_sim_roast_runner_rejects_a_skipped_real_mcp_lane() -> None:
    """The mandatory production MCP subprocess lane cannot pass via pytest skip."""
    text = (
        Path(__file__).resolve().parents[1] / ".claude" / "agents" / "sim-roast-runner.md"
    ).read_text()
    normalized = " ".join(text.split())
    assert "Report pytest's pass/skip counts verbatim" in normalized
    assert "tests/test_milestone1_real_mcp.py` is a gate failure" in normalized


@pytest.mark.docs
def test_runbook_and_skill_and_agents_row_point_to_print_validation_commands() -> None:
    """T17: the runbook, SKILL.md, and the AGENTS.md row cite the single interface.

    None of the three duplicates the ten dynamic per-role command strings —
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


def test_native_review_contracts_fail_closed_on_missing_parent_evidence() -> None:
    """Product and simulation reviewers require complete parent evidence under capture."""
    root = Path(__file__).resolve().parents[1]
    product = " ".join((root / ".claude/agents/product-auditor.md").read_text().split())
    simulation = " ".join((root / ".claude/agents/sim-roast-runner.md").read_text().split())
    assert "`--plan-root`/`--plan-sha` pair is mandatory" in product
    assert "complete exit-status-backed, exact-head gate and coverage evidence" in product
    assert "no admitted native Bash gate" in product
    assert "fail closed when that evidence is absent or partial" in product
    assert "parent-supplied exact-head decision-trace evidence" in simulation
    assert "complete trace rather than a prose summary" in simulation
    assert "Fail closed when required trace evidence is absent or partial" in simulation


@pytest.mark.docs
def test_native_usage_and_evidence_collection_contracts_are_documented() -> None:
    """Runbook and skill retain the external sink, freshness, and pagination boundaries."""
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs/agent-team-worktrees.md").read_text()
    skill = (root / ".agents/skills/capture-agent-usage/SKILL.md").read_text()
    for text in (runbook, skill):
        assert "--usage-root" in text
        assert "ten minutes" in text or "ten-minute" in text
        assert "authors.json" in text
        assert "review-thread" in text
    assert runbook.count("gh api --paginate --slurp") == 3
    assert "gh api graphql --paginate --slurp" in runbook
    assert "missing API author identity" in runbook
    assert "unique_by([.login,.author_association])" in runbook
    assert "re-read the live PR head, checks" in runbook


def test_native_codex_capture_unsupported_status_is_documented() -> None:
    """D206 documents the deliberate unsupported native-Codex capture state."""
    normalized = {
        name: " ".join(path.read_text().split())
        for name, path in {
            "skill": _REPO_ROOT / ".agents" / "skills" / "capture-agent-usage" / "SKILL.md",
            "agents": _REPO_ROOT / "AGENTS.md",
        }.items()
    }
    assert "native Codex capture is currently unsupported for them" in normalized["skill"]
    assert "capture for these roles is unsupported, not widened" in normalized["agents"]
    assert _LIVE_CODEX_MODEL in normalized["skill"]
    assert _LIVE_CODEX_MODEL in normalized["agents"]


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

_PLAN_REQUIRED_ROLES = {
    NativeClaudeRole.PLANNING_ARCHITECT,
    NativeClaudeRole.PRODUCT_AUDITOR,
    NativeClaudeRole.STORY_PLANNER,
}
_PLAN_OPTIONAL_ROLES: set[NativeClaudeRole] = set()
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


def test_native_environment_inherits_only_closed_non_secret_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent credentials and injection variables never reach a native child."""
    inherited = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "LANG": "en_GB.UTF-8",
    }
    credentials = {
        "GH_TOKEN": "gh-secret",
        "GITHUB_TOKEN": "github-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "DATABASE_URL": "database-secret",
        "HTTPS_PROXY": "https://user:secret@proxy.example",
        "NODE_OPTIONS": "--require=/tmp/injected.js",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }
    for key, value in {**inherited, **credentials}.items():
        monkeypatch.setenv(key, value)

    launch_environment = usage_cli._resolve_native_environment(  # pyright: ignore[reportPrivateUsage]
        NativeClaudeRole.ENGINEER_BE, _request(), attested_head="0" * 40
    )

    assert launch_environment.bound_root is None
    assert inherited.items() <= launch_environment.environment.items()
    assert not (set(credentials) & set(launch_environment.environment))
    assert set(launch_environment.environment) <= usage_cli._NATIVE_SAFE_INHERITED_ENVIRONMENT_KEYS  # pyright: ignore[reportPrivateUsage]


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


def _build_evidence_bundle(
    root: Path,
    *,
    pr: int = 837,
    head_sha: str = _EVIDENCE_HEAD,
    base_sha: str = _EVIDENCE_BASE,
    generated_at: str | None = None,
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
        "generated_at": generated_at or usage_cli._utc_now().isoformat(),  # pyright: ignore[reportPrivateUsage]
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


def test_evidence_bundle_pr_json_allows_additional_untrusted_metadata(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Identity is mandatory while useful PR metadata remains available to triage."""
    payload = json.dumps(
        {
            "number": 837,
            "headRefOid": _EVIDENCE_HEAD,
            "baseRefOid": _EVIDENCE_BASE,
            "title": "untrusted PR title",
            "body": "untrusted PR body",
        }
    ).encode()
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-pr-metadata") / "root",
        payload_overrides={"pr.json": payload},
    )
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.kind is BoundRootKind.EVIDENCE


def test_evidence_bundle_rejects_boolean_pr_number(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """JSON ``true`` never aliases integer PR number 1."""
    payload = json.dumps(
        {"number": True, "headRefOid": _EVIDENCE_HEAD, "baseRefOid": _EVIDENCE_BASE}
    ).encode()
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-pr-bool") / "root",
        pr=1,
        payload_overrides={"pr.json": payload},
    )
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 1, attested_head=_EVIDENCE_HEAD
        )


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
    generated_at: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Z`` and explicit ``+00:00`` RFC3339 UTC forms remain valid."""
    monkeypatch.setattr(usage_cli, "_utc_now", lambda: datetime(2025, 1, 1, 0, 5, tzinfo=UTC))
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-utc-generated-at") / "root",
        generated_at=generated_at,
    )
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    assert bound.kind is BoundRootKind.EVIDENCE


@pytest.mark.parametrize(
    ("age_seconds", "accepted"),
    [(600, True), (601, False)],
    ids=["exact-ten-minute-boundary", "one-second-too-old"],
)
def test_evidence_bundle_enforces_conservative_max_age(
    age_seconds: int,
    accepted: bool,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle is valid through exactly ten minutes and stale one second later."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(usage_cli, "_utc_now", lambda: now)
    generated_at = (now - timedelta(seconds=age_seconds)).isoformat()
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-age") / "root", generated_at=generated_at
    )
    if accepted:
        bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
            str(root), 837, attested_head=_EVIDENCE_HEAD
        )
        assert bound.kind is BoundRootKind.EVIDENCE
    else:
        with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
            usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
                str(root), 837, attested_head=_EVIDENCE_HEAD
            )


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
    """T15: a byte-identical root replacement fails identity attestation."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-root-replaced") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    shutil.rmtree(root)
    _build_evidence_bundle(root)
    assert bound.reattest is not None
    with pytest.raises(CaptureUsageError, match="evidence bundle is invalid"):
        bound.reattest()


def test_evidence_bundle_reattest_rejects_content_consistent_snapshot_change(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """T15: reattestation rejects a changed, otherwise valid bundle through its held descriptor."""
    root = _build_evidence_bundle(tmp_path_factory.mktemp("evidence-snapshot-change") / "root")
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    payload_path = root / "authors.json"
    manifest_path = root / EVIDENCE_MANIFEST_NAME
    replacement = b'{"authors.json": "changed"}'
    payload_path.chmod(0o600)
    payload_path.write_bytes(replacement)
    payload_path.chmod(0o400)
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["authors.json"] = {
        "sha256": hashlib.sha256(replacement).hexdigest(),
        "bytes": len(replacement),
    }
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o400)
    assert bound.reattest is not None
    assert bound.descriptor is not None
    usage_cli._evidence_bundle_state(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, _EVIDENCE_HEAD, held_descriptor=bound.descriptor
    )
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


def test_evidence_bundle_reattest_does_not_reapply_launch_freshness(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged bundle admitted near expiry remains valid after a long review."""
    launch_time = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    current_time = launch_time
    monkeypatch.setattr(usage_cli, "_utc_now", lambda: current_time)
    root = _build_evidence_bundle(
        tmp_path_factory.mktemp("evidence-review-duration") / "root",
        generated_at=(launch_time - timedelta(seconds=599)).isoformat(),
    )
    bound = usage_cli._validate_evidence_bundle(  # pyright: ignore[reportPrivateUsage]
        str(root), 837, attested_head=_EVIDENCE_HEAD
    )
    current_time = launch_time + timedelta(minutes=20)
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
    _admit_proven_story_planner_version(monkeypatch)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_proven_story_planner_transcript_bytes(),
        ),
    )
    assert (
        main(_native_cli_args(role="story-planner", plan_root=str(plan_root), plan_sha=plan_sha))
        == 0
    )
    worker_argv = observed[0][0]
    assert worker_argv[worker_argv.index("--add-dir") + 1] == resolved_plan_root
    assert "--allowedTools" not in worker_argv
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.STORY_PLANNER


_STORY_PLANNER_SETTINGS_LITERAL = '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}'


def _transcript_at_version(content: bytes, version: str) -> bytes:
    """Rebind every versioned row of one synthetic transcript to ``version``."""
    rows: list[dict[str, Any]] = [json.loads(line) for line in content.splitlines()]
    for row in rows:
        if "version" in row:
            row["version"] = version
    return _dump_rows(rows)


def _launch_plan_role_at_probed_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    role: str,
    version: str,
    version_output: bytes | None = None,
    transcript: bytes | None = None,
) -> tuple[int | None, str, list[list[str]]]:
    """Run one plan-root native launch whose real version probe reports ``version``.

    The probe emits ``version_output`` verbatim, defaulting to Claude Code's
    observed release spelling of ``version``. ``transcript`` replaces the
    committed role fixture as the worker's parent transcript.

    Returns the exit code (``None`` on refusal), the refusal message, and every
    observed Popen argv (the version probe included).
    """
    plan_root = _build_plan_root(tmp_path_factory.mktemp(f"d219-{role}") / "root")
    plan_sha = "1" * 40
    monkeypatch.setattr(
        usage_cli, "_git_output", _fixed_plan_git(os.path.realpath(plan_root), plan_sha)
    )
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    monkeypatch.setattr(usage_cli, "_harness_version", _REAL_HARNESS_VERSION)
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)
    source = (
        transcript
        if transcript is not None
        else _read_only_transcript_bytes("story-planner")
        if role == "story-planner"
        else _read_only_transcript_bytes_rebranded("security-reviewer", role)
    )
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project,
            observed,
            processes,
            transcript=_transcript_at_version(source, version),
            version_output=(
                f"{version} (Claude Code)\n".encode() if version_output is None else version_output
            ),
        ),
    )
    try:
        code: int | None = main(
            _native_cli_args(role=role, plan_root=str(plan_root), plan_sha=plan_sha)
        )
        message = ""
    except SystemExit as error:
        code = None
        message = str(error)
    return code, message, [argv for argv, _ in observed]


@pytest.mark.parametrize(
    ("version", "admitted"),
    [
        ("2.1.310", True),
        ("2.1.311", False),
        ("2.1.309", False),
        ("2.1.3100", False),
        ("2.1.31", False),
        ("0.0.0", False),
    ],
)
def test_story_planner_version_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    version: str,
    admitted: bool,
) -> None:
    """D219 G2: only an exactly proven version launches native ``story-planner``.

    A refusal happens after the real version probe but before any worker
    Popen, and writes no usage record. Matching is exact membership, so a
    prefix (``2.1.3100``, ``2.1.31``) or a neighbouring version is refused.
    """
    code, message, argvs = _launch_plan_role_at_probed_version(
        tmp_path, monkeypatch, tmp_path_factory, role="story-planner", version=version
    )
    assert argvs[0] == ["claude", "--version"]
    if admitted:
        assert code == 0
        assert len(argvs) == 2 and "--session-id" in argvs[1]
        assert Path(".agent-usage/usage.jsonl").exists()
    else:
        assert code is None
        assert "story-planner read confinement is unproven for this Claude version" in message
        assert argvs == [["claude", "--version"]]
        assert not Path(".agent-usage").exists()


_UNPROVEN_STORY_PLANNER_VERSION_OUTPUTS = (
    b"2.1.310-rc.1 (Claude Code)\n",
    b"2.1.310+local (Claude Code)\n",
    b"2.1.310 (Claude Code)\nextra\n",
    b"2.1.310 (Claude Code)\n\n",
    b"2.1.310 (Claude Code)\xff\n",
    b"\xff2.1.310 (Claude Code)\n",
    b"2.1.310 (Claude Code)",
    b"2.1.310 (Claude Code)\r\n",
    b" 2.1.310 (Claude Code)\n",
    b"Claude Code 2.1.310\n",
    b"2.1.310\n",
    b"2.1.310 (Claude Code) 9.9.9\n",
)
"""Probe outputs whose first ``x.y.z`` is the proven version but whose spelling is not."""


@pytest.mark.parametrize("version_output", _UNPROVEN_STORY_PLANNER_VERSION_OUTPUTS)
def test_story_planner_version_gate_requires_exact_release_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    version_output: bytes,
) -> None:
    """D219 G2 checks the raw bounded probe bytes, not only the first ``x.y.z``.

    A prerelease, build suffix, extra line, undecodable byte, or other spelling
    reduces to ``2.1.310`` under the generic extraction, yet is refused before
    any worker Popen and writes no usage record.
    """
    code, message, argvs = _launch_plan_role_at_probed_version(
        tmp_path,
        monkeypatch,
        tmp_path_factory,
        role="story-planner",
        version="2.1.310",
        version_output=version_output,
    )
    assert code is None
    assert "story-planner read confinement is unproven for this Claude version" in message
    assert argvs == [["claude", "--version"]]
    assert not Path(".agent-usage").exists()


def test_exact_release_spelling_leaves_other_plan_roles_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The raw-spelling check is ``story-planner`` only; others keep D173 extraction."""
    code, message, argvs = _launch_plan_role_at_probed_version(
        tmp_path,
        monkeypatch,
        tmp_path_factory,
        role="product-auditor",
        version="2.1.310",
        version_output=b"2.1.310-rc.1 (Claude Code)\n",
    )
    assert (code, message) == (0, "")
    assert [argv for argv in argvs if argv[-1] == "--version"] == [["claude", "--version"]]
    record = USAGE_RECORD_ADAPTER.validate_json(Path(".agent-usage/usage.jsonl").read_text())
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.harness_version == "2.1.310"


def test_harness_version_probe_returns_the_one_bounded_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single probe returns its extracted version with the exact raw bytes."""
    observed: list[list[str]] = []

    def fake_popen(argv: list[str], **_kwargs: object) -> _NativeProcess:
        observed.append(argv)
        return _NativeProcess(b"2.1.310-rc.1 (Claude Code)\n")

    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    assert _REAL_HARNESS_VERSION_PROBE("/stub/claude") == (
        "2.1.310",
        b"2.1.310-rc.1 (Claude Code)\n",
    )
    monkeypatch.setattr(usage_cli, "_harness_version_probe", _REAL_HARNESS_VERSION_PROBE)
    assert _REAL_HARNESS_VERSION("/stub/claude") == "2.1.310"
    assert observed == [["/stub/claude", "--version"]] * 2


def test_version_gate_leaves_other_plan_roles_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """D219 G2 is ``story-planner`` only: another READ_ONLY role launches on ``2.1.311``."""
    code, message, argvs = _launch_plan_role_at_probed_version(
        tmp_path, monkeypatch, tmp_path_factory, role="product-auditor", version="2.1.311"
    )
    assert (code, message) == (0, "")
    assert len(argvs) == 2 and "--session-id" in argvs[1]
    assert "--settings" not in argvs[1]
    record = USAGE_RECORD_ADAPTER.validate_json(Path(".agent-usage/usage.jsonl").read_text())
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.harness_version == "2.1.311"


def _make_settings_entry(path: Path, shape: str) -> None:
    """Create one project-settings filesystem entry of the named shape."""
    path.parent.mkdir(exist_ok=True)
    if shape == "file":
        path.write_text("{}")
    elif shape == "directory":
        path.mkdir()
    elif shape == "symlink":
        target = path.parent / "settings-target.json"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        assert shape == "dangling-symlink"
        path.symlink_to(path.parent / "absent-target.json")


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize(
    "shape", ["file", "directory", "symlink", "dangling-symlink", "permission-error"]
)
def test_story_planner_project_settings_presence_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    shape: str,
) -> None:
    """D219 G3: any project settings entry, or unprovable absence, refuses the launch.

    The check uses ``lstat``, so a dangling symlink is refused, and every
    refusal happens before the worker Popen and writes no record.
    """
    settings = tmp_path / ".claude" / name
    if shape == "permission-error":
        real_lstat = os.lstat

        def denied_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if Path(path) == Path(".claude") / name:
                raise PermissionError(13, "denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(usage_cli.os, "lstat", denied_lstat)
    else:
        _make_settings_entry(settings, shape)
    code, message, argvs = _launch_plan_role_at_probed_version(
        tmp_path, monkeypatch, tmp_path_factory, role="story-planner", version="2.1.310"
    )
    assert code is None
    assert "native read confinement is invalid" in message
    assert argvs == [["claude", "--version"]]
    assert not Path(".agent-usage").exists()


def test_project_settings_guard_leaves_other_roles_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """D219 G3 is ``story-planner`` only: another plan role launches with a settings file."""
    _make_settings_entry(tmp_path / ".claude" / "settings.json", "file")
    code, _message, argvs = _launch_plan_role_at_probed_version(
        tmp_path, monkeypatch, tmp_path_factory, role="product-auditor", version="2.1.310"
    )
    assert code == 0
    assert len(argvs) == 2 and "--session-id" in argvs[1]


def test_story_planner_launch_is_confined_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """D219 U7: the real launch carries the setting after ``project`` and records 2.1.310.

    No ``.claude/settings*.json`` entry exists, so G3 passes and the launch
    proceeds.
    """
    code, _message, argvs = _launch_plan_role_at_probed_version(
        tmp_path, monkeypatch, tmp_path_factory, role="story-planner", version="2.1.310"
    )
    assert code == 0
    worker = argvs[1]
    index = worker.index("--settings")
    assert worker[index - 2 : index + 3] == [
        "--setting-sources",
        "project",
        "--settings",
        _STORY_PLANNER_SETTINGS_LITERAL,
        "-p",
    ]
    assert worker.count("--settings") == 1
    record = USAGE_RECORD_ADAPTER.validate_json(Path(".agent-usage/usage.jsonl").read_text())
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.STORY_PLANNER
    assert record.harness_version == "2.1.310"


_ENVELOPE_PAYLOAD_SENTINEL = "SENTINEL_ENVELOPE_PAYLOAD"
"""Marker planted in nested envelope payloads; it must never reach a result or record."""


def _sanitized_envelope_rows(session_id: str) -> list[dict[str, Any]]:
    """Return the lead-supplied 2.1.310 envelope rows bound to ``session_id``.

    Each nested request payload gains one planted sentinel string so a test
    can prove the parser never retains payload content.
    """
    content = (FIXTURES / "claude-2.1.310-envelope-rows.jsonl").read_bytes()
    rows: list[dict[str, Any]] = [json.loads(line) for line in content.splitlines()]
    assert [row["type"] for row in rows] == [
        "api-request-shape",
        "api-request-blob",
        "api-request",
        "api-request",
        "cost-state",
    ]
    for row in rows:
        row["sessionId"] = session_id
    rows[0]["shape"]["system"][0]["text"] = _ENVELOPE_PAYLOAD_SENTINEL
    rows[1]["message"]["content"][0]["text"] = _ENVELOPE_PAYLOAD_SENTINEL
    rows[2]["params"]["model"] = _ENVELOPE_PAYLOAD_SENTINEL
    rows[3]["tail"][0] = _ENVELOPE_PAYLOAD_SENTINEL
    return rows


def _story_planner_transcript_with_envelopes(
    session_id: str, envelopes: list[dict[str, Any]] | None = None
) -> bytes:
    """Interleave envelope rows through a valid proven-version ``story-planner`` transcript.

    The rows are spread before, between, and after the base fixture's rows,
    as a live 2.1.310 transcript interleaves them with conversation rows.
    """
    base: list[dict[str, Any]] = [
        json.loads(line)
        for line in _proven_story_planner_transcript_bytes(session_id=session_id).splitlines()
    ]
    inserted = _sanitized_envelope_rows(session_id) if envelopes is None else envelopes
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(base):
        if index < len(inserted):
            rows.append(inserted[index])
        rows.append(row)
    rows.extend(inserted[len(base) :])
    return _dump_rows(rows)


def _parse_story_planner_transcript(
    tmp_path: Path, session_id: str
) -> usage_transcript.TranscriptUsage:
    """Parse one installed ``story-planner`` transcript under the D219 proven version."""
    return usage_transcript.parse_owned_transcript(
        tmp_path,
        session_id,
        NativeClaudeRole.STORY_PLANNER,
        usage_cli._native_role_pin(NativeClaudeRole.STORY_PLANNER).effort,  # pyright: ignore[reportPrivateUsage]
        expected_version=_PROVEN_STORY_PLANNER_VERSION,
        expected_permission_mode="dontAsk",
        require_handback=True,
    )


def test_observed_2_1_310_envelope_rows_validate_and_are_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D219 U9: the sanitized 2.1.310 envelopes parse without changing or retaining anything.

    Usage, model, call count, and handback equal the envelope-free baseline,
    even when ``cost-state`` reports different per-model token counts, and no
    nested payload string is retained.
    """
    session_id = "73333333-3333-4333-8333-333333333333"
    transcript, installed = _install_owned_transcript(
        tmp_path,
        monkeypatch,
        _proven_story_planner_transcript_bytes(session_id=session_id),
        session_id,
    )
    baseline = _parse_story_planner_transcript(tmp_path, installed)
    envelopes = _sanitized_envelope_rows(session_id)
    envelopes[4]["modelUsage"]["claude-opus-5-5"]["inputTokens"] = 999_999
    envelopes[4]["totalCostUSD"] = 1.25
    transcript.write_bytes(_story_planner_transcript_with_envelopes(session_id, envelopes))

    usage = _parse_story_planner_transcript(tmp_path, installed)

    assert usage == baseline
    assert usage.handback_text == "SYNTHETIC_STORY_HANDOFF"
    assert _ENVELOPE_PAYLOAD_SENTINEL not in repr(usage)
    assert "999999" not in repr(usage)


def test_envelope_rows_alone_never_complete_a_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Envelope rows contribute no usage: without an assistant row the transcript is incomplete."""
    session_id = "74444444-4444-4444-8444-444444444444"
    rows = [
        json.loads(line)
        for line in _story_planner_transcript_with_envelopes(session_id).splitlines()
    ]
    kept = [row for row in rows if row.get("type") != "assistant"]
    _install_owned_transcript(tmp_path, monkeypatch, _dump_rows(kept), session_id)
    with pytest.raises(usage_transcript.TranscriptError, match="incomplete"):
        _parse_story_planner_transcript(tmp_path, session_id)


def _envelope_mutation(
    index: int, mutate: Callable[[dict[str, Any]], None]
) -> Callable[[list[dict[str, Any]]], None]:
    """Return a transform that applies ``mutate`` to one sanitized envelope row."""

    def apply(rows: list[dict[str, Any]]) -> None:
        mutate(rows[index])

    return apply


def _set(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(row: dict[str, Any]) -> None:
        row[key] = value

    return mutate


def _drop(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(row: dict[str, Any]) -> None:
        del row[key]

    return mutate


def _set_model_usage(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(row: dict[str, Any]) -> None:
        row["modelUsage"]["claude-opus-5-5"][key] = value

    return mutate


def _drop_model_usage(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(row: dict[str, Any]) -> None:
        del row["modelUsage"]["claude-opus-5-5"][key]

    return mutate


_SHAPE, _BLOB, _REQUEST, _REQUEST_BASE, _COST = range(5)
_MALICIOUS_ENVELOPES: dict[str, Callable[[list[dict[str, Any]]], None]] = {
    "unknown-type": _envelope_mutation(_BLOB, _set("type", "api-request-extra")),
    "unknown-prefixed-type": _envelope_mutation(_COST, _set("type", "cost-state-v2")),
    "shape-extra-key": _envelope_mutation(_SHAPE, _set("agentId", "x")),
    "blob-extra-base": _envelope_mutation(_BLOB, _set("base", "SYNTHETIC")),
    "request-extra-usage": _envelope_mutation(_REQUEST, _set("usage", {"input_tokens": 1})),
    "cost-extra-key": _envelope_mutation(_COST, _set("subagents", [])),
    "shape-permission-mode": _envelope_mutation(_SHAPE, _set("permissionMode", "dontAsk")),
    "shape-missing-shape": _envelope_mutation(_SHAPE, _drop("shape")),
    "shape-missing-version": _envelope_mutation(_SHAPE, _drop("version")),
    "shape-missing-timestamp": _envelope_mutation(_SHAPE, _drop("timestamp")),
    "blob-missing-message": _envelope_mutation(_BLOB, _drop("message")),
    "blob-missing-hash": _envelope_mutation(_BLOB, _drop("hash")),
    "request-missing-params": _envelope_mutation(_REQUEST, _drop("params")),
    "request-missing-version": _envelope_mutation(_REQUEST_BASE, _drop("version")),
    "request-missing-digest": _envelope_mutation(_REQUEST_BASE, _drop("requestDigest")),
    "cost-missing-model-usage": _envelope_mutation(_COST, _drop("modelUsage")),
    "cost-missing-flag": _envelope_mutation(_COST, _drop("hasUnknownModelCost")),
    "missing-session": _envelope_mutation(_BLOB, _drop("sessionId")),
    "foreign-session": _envelope_mutation(_COST, _set("sessionId", "foreign")),
    "shape-version-drift": _envelope_mutation(_SHAPE, _set("version", "2.1.311")),
    "request-version-drift": _envelope_mutation(_REQUEST, _set("version", "2.1.3100")),
    "shape-version-number": _envelope_mutation(_SHAPE, _set("version", 2)),
    "shape-bad-timestamp": _envelope_mutation(_SHAPE, _set("timestamp", "not-a-time")),
    "request-numeric-timestamp": _envelope_mutation(_REQUEST, _set("timestamp", 0)),
    "shape-list-shape": _envelope_mutation(_SHAPE, _set("shape", [])),
    "blob-string-message": _envelope_mutation(_BLOB, _set("message", "SYNTHETIC")),
    "blob-null-hash": _envelope_mutation(_BLOB, _set("hash", None)),
    "request-list-params": _envelope_mutation(_REQUEST, _set("params", [])),
    "request-object-tail": _envelope_mutation(_REQUEST, _set("tail", {})),
    "request-object-cache-control": _envelope_mutation(_REQUEST, _set("cacheControl", {})),
    "request-numeric-id": _envelope_mutation(_REQUEST, _set("id", 1)),
    "request-numeric-base": _envelope_mutation(_REQUEST_BASE, _set("base", 0)),
    "request-bool-keep": _envelope_mutation(_REQUEST, _set("keep", True)),
    "request-negative-keep": _envelope_mutation(_REQUEST, _set("keep", -1)),
    "request-fractional-keep": _envelope_mutation(_REQUEST, _set("keep", 1.5)),
    "cost-string-amount": _envelope_mutation(_COST, _set("totalCostUSD", "0")),
    "cost-negative-amount": _envelope_mutation(_COST, _set("totalDuration", -1)),
    "cost-bool-amount": _envelope_mutation(_COST, _set("startTime", True)),
    "cost-infinite-amount": _envelope_mutation(_COST, _set("totalAPIDuration", math.inf)),
    "cost-nan-amount": _envelope_mutation(_COST, _set("totalToolDuration", math.nan)),
    "cost-fractional-count": _envelope_mutation(_COST, _set("totalLinesAdded", 1.5)),
    "cost-integer-flag": _envelope_mutation(_COST, _set("hasUnknownModelCost", 0)),
    "cost-list-model-usage": _envelope_mutation(_COST, _set("modelUsage", [])),
    "cost-model-usage-not-object": _envelope_mutation(
        _COST, _set("modelUsage", {"claude-opus-5-5": 0})
    ),
    "cost-model-usage-extra-key": _envelope_mutation(_COST, _set_model_usage("prompt", "x")),
    "cost-model-usage-extra-numeric-key": _envelope_mutation(
        _COST, _set_model_usage("contextWindow", 0)
    ),
    "cost-model-usage-missing-key": _envelope_mutation(_COST, _drop_model_usage("costUSD")),
    "cost-model-usage-negative": _envelope_mutation(_COST, _set_model_usage("inputTokens", -1)),
    "cost-model-usage-bool": _envelope_mutation(_COST, _set_model_usage("outputTokens", False)),
    "cost-model-usage-string": _envelope_mutation(_COST, _set_model_usage("costUSD", "0")),
}
"""Malicious or malformed variants of the sanitized 2.1.310 envelopes; each must fail closed."""


@pytest.mark.parametrize("variant", sorted(_MALICIOUS_ENVELOPES))
def test_malformed_2_1_310_envelope_rows_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """D219 U9: unknown types and every malformed envelope reject the whole transcript."""
    session_id = "75555555-5555-4555-8555-555555555555"
    envelopes = _sanitized_envelope_rows(session_id)
    _MALICIOUS_ENVELOPES[variant](envelopes)
    _install_owned_transcript(
        tmp_path,
        monkeypatch,
        _story_planner_transcript_with_envelopes(session_id, envelopes),
        session_id,
    )
    with pytest.raises(
        usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
    ):
        _parse_story_planner_transcript(tmp_path, session_id)


def test_envelope_payload_duplicate_key_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate key nested inside an unread envelope payload still rejects the row."""
    session_id = "76666666-6666-4666-8666-666666666666"
    content = _story_planner_transcript_with_envelopes(session_id)
    tampered = content.replace(
        b'"params": {"model": ', b'"params": {"model": "SYNTHETIC", "model": ', 1
    )
    assert tampered != content
    _install_owned_transcript(tmp_path, monkeypatch, tampered, session_id)
    with pytest.raises(
        usage_transcript.TranscriptError, match="owned Claude transcript is invalid"
    ):
        _parse_story_planner_transcript(tmp_path, session_id)


def test_story_planner_launch_records_usage_with_2_1_310_envelope_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """D219 U9: a confined launch whose transcript carries 2.1.310 envelopes appends its record.

    The record carries the role, version, and fixture usage, and no envelope
    payload string reaches the usage record or the handback stdout line.
    """
    code, message, _argvs = _launch_plan_role_at_probed_version(
        tmp_path,
        monkeypatch,
        tmp_path_factory,
        role="story-planner",
        version=_PROVEN_STORY_PLANNER_VERSION,
        transcript=_story_planner_transcript_with_envelopes(_NATIVE_SESSION_ID),
    )
    assert (code, message) == (0, "")
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.STORY_PLANNER
    assert record.harness_version == _PROVEN_STORY_PLANNER_VERSION
    assert record.success and record.usage_complete
    assert _ENVELOPE_PAYLOAD_SENTINEL not in raw


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
    worker_argv = observed[0][0]
    assert worker_argv[worker_argv.index("--add-dir") + 1] == os.path.realpath(evidence_root)
    assert "--allowedTools" not in worker_argv
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.native_role is NativeClaudeRole.PR_TRIAGE


def test_native_launch_product_auditor_rejects_missing_plan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """T8 (roster): ``product-auditor`` fails closed without its required plan pair."""
    del tmp_path_factory
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
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(_native_cli_args(role="product-auditor"))
    assert observed == []


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
    bound_argv = observed[0][0]
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


def test_excluded_assurance_role_never_reaches_native_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T22/L11: an excluded assurance role cannot reach native launch argv."""
    project, observed = _configure_read_only_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(
            project, observed, processes, transcript=_read_only_transcript_bytes("safety-reviewer")
        ),
    )
    with pytest.raises(SystemExit):
        main(_native_cli_args(role="safety-reviewer"))
    assert observed == []
    assert processes == []


# --- print-validation-commands: ALLOW / RUN grammar (D169, §2.5) -----------


def test_print_validation_commands_allow_then_run_blocks(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T18: output is exactly the ``ALLOW`` block then the ``RUN`` block; every RUN is covered."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    for role in usage_cli.VALIDATION_ENVIRONMENT_ROLES:
        args = [
            "print-validation-commands",
            "--role",
            role.value,
            "--validation-root",
            str(root),
        ]
        tokens: tuple[str, ...] = ()
        if role is NativeClaudeRole.QA:
            tokens = ("tests/", "--cov=src", "--cov-branch", "--cov-report=term-missing")
            for token in tokens:
                args.append(f"--pytest-arg={token}")
        exit_code = main(args)
        assert exit_code == 0
        printed = capsys.readouterr().out.splitlines()
        rendered = render_validation_commands(role, resolved_root)
        commands = VALIDATION_ROLE_COMMANDS[role]
        allow_lines = [
            f"ALLOW {command.kind.value} {text}"
            for command, text in zip(commands, rendered, strict=True)
        ]
        run_lines = [
            f"RUN {usage_cli._render_prefix_run_command(text, tokens)}"  # pyright: ignore[reportPrivateUsage]
            if command.kind is ValidationCommandKind.PREFIX
            else f"RUN {text}"
            for command, text in zip(commands, rendered, strict=True)
        ]
        assert printed == [*allow_lines, *run_lines]
        assert all(line.startswith("ALLOW ") for line in printed[: len(allow_lines)])
        assert all(line.startswith("RUN ") for line in printed[len(allow_lines) :])


def test_print_validation_commands_pytest_arg_renders_quoted_covered_run_line(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T19: tokens with space, quote, ``$``, ``;``, and a glob each round-trip through shlex."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    resolved_root = os.path.realpath(root)
    tokens = [
        "tests/has space.py",
        "--cov=.agents/skills/capture-agent-usage/scripts",
        "--cov-branch",
        "--cov-report=term-missing",
        "it's",
        "$HOME",
        ";rm -rf",
        "tests/*.py",
    ]
    args = ["print-validation-commands", "--role", "qa", "--validation-root", str(root)]
    for token in tokens:
        args.append(f"--pytest-arg={token}")
    assert main(args) == 0
    printed = capsys.readouterr().out.splitlines()
    prefix = render_validation_commands(NativeClaudeRole.QA, resolved_root)[1]
    run_line = next(line for line in printed if line.startswith("RUN " + prefix))
    run_command = run_line.removeprefix("RUN ")
    assert shlex.split(run_command) == [*shlex.split(prefix), *tokens]
    assert run_command.startswith(prefix + " ")


def test_print_validation_commands_accepts_explicit_coverage_capable_qa_args(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA accepts a non-empty focused test target with branch-capable coverage flags."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    tokens = [
        "tests/test_capture_agent_usage.py",
        "--cov=.agents/skills/capture-agent-usage/scripts",
        "--cov-branch",
        "--cov-report=term-missing",
    ]
    args = ["print-validation-commands", "--role", "qa", "--validation-root", str(root)]
    for token in tokens:
        args.append(f"--pytest-arg={token}")
    assert main(args) == 0
    prefix = render_validation_commands(NativeClaudeRole.QA, os.path.realpath(root))[1]
    run_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("RUN " + prefix)
    )
    assert shlex.split(run_line.removeprefix("RUN "))[-len(tokens) :] == tokens


def test_print_validation_commands_qa_rejects_omitted_tokens_without_output(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """T19: QA rejects an empty pytest target rather than emitting bare pytest."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(["print-validation-commands", "--role", "qa", "--validation-root", str(root)])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("token", ["", "   "])
def test_print_validation_commands_qa_rejects_empty_or_whitespace_token(
    token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA rejects empty framing tokens before root validation or output."""
    with pytest.raises(SystemExit):
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                "/validated/root",
                "--pytest-arg",
                token,
            ]
        )
    captured = capsys.readouterr()
    assert "pytest argument token contains a forbidden byte" in captured.err
    assert captured.out == ""


def test_print_validation_commands_qa_rejects_flags_without_test_selector(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA coverage flags do not substitute for an explicit repository test selector."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                str(root),
                "--pytest-arg=--cov=src",
                "--pytest-arg=--cov-report=term-missing",
            ]
        )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "tokens",
    [
        ["tests/test_capture_agent_usage.py"],
        [
            "tests/test_capture_agent_usage.py",
            "--cov=",
            "--cov-branch",
            "--cov-report=term-missing",
        ],
        ["tests/test_capture_agent_usage.py", "--cov=src", "--cov-report=term-missing"],
        ["tests/test_capture_agent_usage.py", "--cov=src", "--cov-branch"],
    ],
)
def test_print_validation_commands_qa_rejects_incomplete_coverage_contract(
    tokens: list[str], tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA never emits a RUN line unless selector and all required coverage tokens are present."""
    root = _build_validation_root(tmp_path_factory.mktemp("base") / "root")
    args = ["print-validation-commands", "--role", "qa", "--validation-root", str(root)]
    for token in tokens:
        args.append(f"--pytest-arg={token}")
    with pytest.raises(SystemExit, match="validation environment is invalid"):
        main(args)
    assert capsys.readouterr().out == ""


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
                "--pytest-arg=tests/",
                "--pytest-arg=--cov=src",
                "--pytest-arg=--cov-branch",
                "--pytest-arg=--cov-report=term-missing",
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


def test_shlex_split_rejects_malformed_quoted_input() -> None:
    """The defensive shell splitter converts malformed quoting to the fixed error."""
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._shlex_split("unterminated 'quote")  # pyright: ignore[reportPrivateUsage]


def test_render_prefix_run_command_rejects_broken_coverage_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M22: a quote mutation reaches and defeats the renderer round-trip proof."""

    def identity_quote(value: str) -> str:
        return value

    monkeypatch.setattr(usage_cli.shlex, "quote", identity_quote)
    with pytest.raises(CaptureUsageError, match="validation environment is invalid"):
        usage_cli._render_prefix_run_command(  # pyright: ignore[reportPrivateUsage]
            "python -m pytest", ("tests/has space.py",)
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
                "tests/has space.py",
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
    assert (
        main(
            [
                "print-validation-commands",
                "--role",
                "qa",
                "--validation-root",
                str(root),
                "--pytest-arg=tests/",
                "--pytest-arg=--cov=src",
                "--pytest-arg=--cov-branch",
                "--pytest-arg=--cov-report=term-missing",
            ]
        )
        == 0
    )
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


@pytest.mark.docs
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
    # D219 U5: the confinement option and its literal each exist exactly once,
    # and only the native argv builder reads the constant carrying the literal.
    literal = "'" + _STORY_PLANNER_SETTINGS_LITERAL + "'"
    constant = "_STORY_PLANNER_READ_CONFINEMENT_SETTINGS"
    assert cli_text.count('"--settings"') == 1
    assert cli_text.count(literal) == 1
    assert cli_text.count(constant) == 2
    assert cli_text.index(constant) < cli_text.index(literal) < generic_argv_index
    native_end = cli_text.index("\ndef ", native_argv_index + 1)
    native_body = cli_text[native_argv_index:native_end]
    assert native_body.count('"--settings"') == 1
    assert native_body.count(constant) == 1


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


@pytest.mark.slow
def test_nested_witness_failure_evidence_native_shell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """N1: full private streams supplement the unchanged bounded exception tail."""
    calls: list[tuple[str, ...]] = []
    results: list[subprocess.CompletedProcess[str]] = []
    snapshots: list[tuple[tuple[str, ...], int, str, str]] = []

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        result = _run_gate_command(
            'for i in {1..120}; do case "$i" in 1) s=EARLY;; 60) s=MIDDLE;; '
            '120) s=LATE;; *) s=line;; esac; echo "$i:$s:out"; '
            'echo "$i:$s:err" >&2; done; exit 1',
            {},
            cwd=tmp_path,
        )
        snapshots.append((tuple(result.args), result.returncode, result.stdout, result.stderr))
        results.append(result)
        return result

    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    with pytest.raises(_WitnessContractError, match="collection exited nonzero") as actual:
        _run_partitioned_witness(_retain_failed_child_streams(run, evidence))
    result = results[0]
    before = snapshots[0]
    assert (tuple(result.args), result.returncode, result.stdout, result.stderr) == before
    baseline = subprocess.CompletedProcess(*before)
    assert result.returncode == 1
    with pytest.raises(_WitnessContractError) as original:
        _parse_collection(baseline)
    assert str(actual.value) == str(original.value)
    assert calls == [_NW_EXPECTED_CALLS[0]]
    assert all(value not in str(actual.value) for value in ("EARLY", "MIDDLE", str(tmp_path)))
    metadata = json.loads((evidence / "child-00.json").read_text())
    assert set(metadata) == {"schema", "sequence", "exit", "representation", "complete", "streams"}
    assert metadata["schema"] == _WITNESS_FAILURE_SCHEMA
    assert metadata["sequence"] == 0 and metadata["exit"] == 1 and metadata["complete"]
    assert metadata["representation"] == "utf8-of-decoded-universal-newline-text"
    for name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        data = text.encode("utf-8")
        assert (evidence / f"child-00.{name}").read_bytes() == data
        assert all(value in data for value in (b"EARLY", b"MIDDLE", b"LATE"))
        assert metadata["streams"][name] == {
            "retained": True,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "reason": "ok",
        }
    assert capsys.readouterr() == ("", "")


@pytest.mark.slow
def test_nested_witness_failure_evidence_native_pytest(tmp_path: Path) -> None:
    """N2: a real pytest failure retains output preceding the exception's tail."""
    run = _nw_real_child_runner(
        tmp_path,
        "def test_fail():\n    for i in range(120):\n"
        '        print(f"{i}:" + {0: "EARLY", 59: "MIDDLE", 119: "LATE"}.get(i, "line"))\n'
        "    assert False\n",
    )
    result = _retain_failed_child_streams(run, tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR)(("tests",))
    assert result.returncode == 1
    with pytest.raises(_WitnessContractError, match="execution exited nonzero"):
        _require_payload_outcome(result, 1)
    data = (tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR / "child-00.stdout").read_bytes()
    assert all(value in data for value in (b"EARLY", b"MIDDLE", b"LATE"))


@pytest.mark.slow
def test_nested_witness_failure_evidence_success(tmp_path: Path) -> None:
    """N3: passing native results retain object identity and create nothing."""
    result = _run_gate_command("printf passed", {}, cwd=tmp_path)
    assert result.returncode == 0
    before = (tuple(result.args), result.returncode, result.stdout, result.stderr)
    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    assert _retain_failed_child_streams(lambda _: result, evidence)(()) is result
    assert (tuple(result.args), result.returncode, result.stdout, result.stderr) == before
    assert not evidence.exists()


@pytest.mark.parametrize("index", range(10))
def test_nested_witness_failure_evidence_order(tmp_path: Path, index: int) -> None:
    """N4: every failure position preserves exact calls, identity and parser message."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[index]] = _nw_completed("failure", 1)
    before = {
        argv: (tuple(value.args), value.returncode, value.stdout, value.stderr)
        for argv, value in outputs.items()
    }
    baselines = {argv: subprocess.CompletedProcess(*snapshot) for argv, snapshot in before.items()}
    run, calls = _nw_fake_runner(outputs)
    wrapped = _retain_failed_child_streams(run, tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR)

    def checked(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        result = wrapped(argv)
        assert result is outputs[argv]
        assert (tuple(result.args), result.returncode, result.stdout, result.stderr) == before[argv]
        return result

    with pytest.raises(_WitnessContractError) as actual:
        _run_partitioned_witness(checked)
    plain, _ = _nw_fake_runner(baselines)
    with pytest.raises(_WitnessContractError) as original:
        _run_partitioned_witness(plain)
    assert str(actual.value) == str(original.value)
    assert calls == list(_NW_EXPECTED_CALLS[: index + 1])
    assert {path.name for path in (tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR).iterdir()} == {
        f"child-{index:02d}.{suffix}" for suffix in ("stdout", "stderr", "json")
    }


@pytest.mark.parametrize("trailer", ["malformed", "1 failed, 2 passed in 0.01s"])
def test_nested_witness_failure_evidence_zero_exit_rejected(tmp_path: Path, trailer: str) -> None:
    """N5: malformed and masked-success proofs fail without retaining success output."""
    outputs = _nw_default_outputs()
    outputs[_NW_EXPECTED_CALLS[7]] = _nw_completed(_nw_execution(trailer))
    before = {
        argv: (tuple(value.args), value.returncode, value.stdout, value.stderr)
        for argv, value in outputs.items()
    }
    baselines = {argv: subprocess.CompletedProcess(*snapshot) for argv, snapshot in before.items()}
    run, _ = _nw_fake_runner(outputs)
    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    with pytest.raises(_WitnessContractError) as actual:
        _run_partitioned_witness(_retain_failed_child_streams(run, evidence))
    assert {
        argv: (tuple(value.args), value.returncode, value.stdout, value.stderr)
        for argv, value in outputs.items()
    } == before
    plain, _ = _nw_fake_runner(baselines)
    with pytest.raises(_WitnessContractError) as original:
        _run_partitioned_witness(plain)
    assert str(actual.value) == str(original.value)
    assert not evidence.exists()


@pytest.mark.parametrize(
    "text,limit,retained", [("abc", 3, True), ("abcd", 3, False), ("é", 2, True), ("é", 1, False)]
)
def test_nested_witness_failure_evidence_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, limit: int, retained: bool
) -> None:
    """N6/N7: whole UTF-8 streams fit exactly or are honestly marked overflow."""
    monkeypatch.setitem(globals(), "_WITNESS_FAILURE_STREAM_LIMIT", limit)
    result = subprocess.CompletedProcess((), 1, text, "")
    _write_failed_child_evidence(tmp_path, 0, result)
    metadata = json.loads((tmp_path / "child-00.json").read_text())
    assert metadata["complete"] is retained
    assert metadata["streams"]["stdout"] == {
        "retained": retained,
        "bytes": len(text.encode("utf-8")),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "reason": "ok" if retained else "overflow",
    }
    assert (tmp_path / "child-00.stdout").exists() is retained
    if retained:
        assert (tmp_path / "child-00.stdout").read_bytes() == text.encode("utf-8")


def test_nested_witness_failure_evidence_modes(tmp_path: Path) -> None:
    """N8: the directory and every retained file exclude group and other access."""
    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    _retain_failed_child_streams(lambda _: _nw_completed("failure", 1), evidence)(())
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in evidence.iterdir())


@pytest.mark.parametrize("symlink", [False, True])
def test_nested_witness_failure_evidence_existing_path(
    tmp_path: Path, symlink: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """N9: a pre-existing directory or symlink preserves all original bytes."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "preserve").write_text("unchanged")
    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    if symlink:
        evidence.symlink_to(target, target_is_directory=True)
    else:
        evidence.mkdir()
        (evidence / "preserve").write_text("unchanged")
    result = _nw_completed("failure", 1)
    before = (tuple(result.args), result.returncode, result.stdout, result.stderr)
    baseline = subprocess.CompletedProcess(*before)
    assert _retain_failed_child_streams(lambda _: result, evidence)(()) is result
    assert (tuple(result.args), result.returncode, result.stdout, result.stderr) == before
    assert {p.name for p in target.iterdir()} == {"preserve"}
    assert {p.name for p in evidence.iterdir()} == {"preserve"}
    assert (evidence / "preserve").read_text() == "unchanged"
    assert evidence.is_symlink() is symlink
    with pytest.raises(_WitnessContractError) as actual:
        _parse_collection(result)
    with pytest.raises(_WitnessContractError) as original:
        _parse_collection(baseline)
    assert str(actual.value) == str(original.value)
    assert capsys.readouterr() == ("", "")


def test_nested_witness_failure_evidence_no_overwrite(tmp_path: Path) -> None:
    """N10: repeated writer calls and symlink targets remain byte-identical."""
    _write_failed_child_evidence(tmp_path, 0, _nw_completed("first", 1))
    expected_names = {"child-00.stdout", "child-00.stderr", "child-00.json"}
    assert {path.name for path in tmp_path.iterdir()} == expected_names
    stdout_before = (tmp_path / "child-00.stdout").read_bytes()
    stderr_before = (tmp_path / "child-00.stderr").read_bytes()
    metadata_before = (tmp_path / "child-00.json").read_bytes()
    _write_failed_child_evidence(tmp_path, 0, _nw_completed("second", 2))
    assert {path.name for path in tmp_path.iterdir()} == expected_names
    assert (tmp_path / "child-00.stdout").read_bytes() == stdout_before
    assert (tmp_path / "child-00.stderr").read_bytes() == stderr_before
    assert (tmp_path / "child-00.json").read_bytes() == metadata_before
    target = tmp_path / "target"
    target.write_text("preserve")
    (tmp_path / "child-01.stdout").symlink_to(target)
    _write_failed_child_evidence(tmp_path, 1, _nw_completed("second", 1))
    assert target.read_text() == "preserve"
    assert not (tmp_path / "child-01.json").exists()


@pytest.mark.slow
@pytest.mark.parametrize("fault", ["stderr", "missing", "file", "unicode"])
def test_nested_witness_failure_evidence_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str
) -> None:
    """N11: partial evidence never masks the original failure or attests completion."""
    result = _run_gate_command("printf original; printf error >&2; exit 1", {}, cwd=tmp_path)
    if fault == "unicode":
        result = subprocess.CompletedProcess(result.args, 1, "bad\ud800", result.stderr)
    before = (tuple(result.args), result.returncode, result.stdout, result.stderr)
    baseline = subprocess.CompletedProcess(*before)
    evidence = tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR
    if fault == "missing":
        evidence = tmp_path / "missing" / _WITNESS_FAILURE_EVIDENCE_DIR
    if fault == "file":
        (tmp_path / "file").write_text("preserve")
        evidence = tmp_path / "file" / _WITNESS_FAILURE_EVIDENCE_DIR
    original_open = os.open

    def open_file(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if fault == "stderr" and str(path).endswith(".stderr"):
            raise OSError(errno.ENOSPC, "full")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", open_file)
        assert _retain_failed_child_streams(lambda _: result, evidence)(()) is result
        if fault in {"stderr", "unicode"}:
            direct = tmp_path / "direct-writer"
            direct.mkdir()
            _write_failed_child_evidence(direct, 0, result)
            assert not (direct / "child-00.json").exists()
    assert (tuple(result.args), result.returncode, result.stdout, result.stderr) == before
    assert not (evidence / "child-00.json").exists()
    with pytest.raises(_WitnessContractError) as actual:
        _parse_collection(result)
    with pytest.raises(_WitnessContractError) as original:
        _parse_collection(baseline)
    assert str(actual.value) == str(original.value)
    assert str(evidence) not in str(actual.value)
    assert capsys.readouterr() == ("", "")


def test_nested_witness_failure_evidence_first_attempt_latched(tmp_path: Path) -> None:
    """The first failure latches retention even when that attempt cannot create files."""
    evidence = tmp_path / "missing" / _WITNESS_FAILURE_EVIDENCE_DIR
    result = _nw_completed("failure", 1)
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return result

    wrapped = _retain_failed_child_streams(run, evidence)
    assert wrapped(("first",)) is result
    evidence.parent.mkdir()
    assert wrapped(("second",)) is result
    assert not evidence.exists()
    assert calls == [("first",), ("second",)]


def test_nested_witness_failure_evidence_caller_wiring() -> None:
    """N12: only the witness's runner argument gains the private consumer adapter."""
    tree = ast.parse(
        inspect.getsource(test_rendered_qa_full_suite_gate_runs_under_the_actual_wrapper)
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_partitioned_witness"
    ]
    assert len(calls) == 1 and len(calls[0].args) == 1
    wrapper = calls[0].args[0]
    assert isinstance(wrapper, ast.Call) and isinstance(wrapper.func, ast.Name)
    assert wrapper.func.id == "_retain_failed_child_streams" and len(wrapper.args) == 2
    assert ast.unparse(wrapper.args[0]) == (
        "lambda argv: _run_gate_command(f'{wrapper} {shlex.join(argv)}', "
        "environment, cwd=repository)"
    )
    assert ast.unparse(wrapper.args[1]) == "tmp_path / _WITNESS_FAILURE_EVIDENCE_DIR"


def _story_planner_transcript_with_usage_extra(session_id: str, key: str, value: object) -> bytes:
    """Return the envelope-bearing transcript with ``key`` added to every assistant usage."""
    rows = [
        json.loads(line)
        for line in _story_planner_transcript_with_envelopes(session_id).splitlines()
    ]
    assistants = [row for row in rows if row.get("type") == "assistant"]
    assert assistants
    for row in assistants:
        row["message"]["usage"][key] = value
    return _dump_rows(rows)


def test_observed_2_1_310_null_fallback_credit_is_admitted_and_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D219 U9: a JSON-null ``fallback_credit`` parses and leaves usage and handback unchanged.

    Deleting the null-only admission makes the key unknown, so this parse fails.
    """
    assert frozenset({"fallback_credit"}) == usage_transcript._USAGE_NULL_ONLY  # pyright: ignore[reportPrivateUsage]
    assert "fallback_credit" not in usage_transcript._USAGE_OPTIONAL  # pyright: ignore[reportPrivateUsage]
    session_id = "75555555-5555-4555-8555-555555555555"
    transcript, installed = _install_owned_transcript(
        tmp_path, monkeypatch, _story_planner_transcript_with_envelopes(session_id), session_id
    )
    baseline = _parse_story_planner_transcript(tmp_path, installed)
    transcript.write_bytes(
        _story_planner_transcript_with_usage_extra(session_id, "fallback_credit", None)
    )

    usage = _parse_story_planner_transcript(tmp_path, installed)

    assert usage == baseline
    assert usage.handback_text == "SYNTHETIC_STORY_HANDOFF"
    assert "fallback_credit" not in repr(usage)


@pytest.mark.parametrize(
    "key,value",
    [
        ("fallback_credit", 0),
        ("fallback_credit", 1),
        ("fallback_credit", 0.5),
        ("fallback_credit", "0"),
        ("fallback_credit", ""),
        ("fallback_credit", False),
        ("fallback_credit", True),
        ("fallback_credit", {}),
        ("fallback_credit", {"amount": None}),
        ("fallback_credit", []),
        ("fallback_credit", [None]),
        ("fallback_credits", None),
        ("other_credit", None),
    ],
)
def test_non_null_fallback_credit_or_other_null_usage_key_is_rejected(
    key: str, value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact key ``fallback_credit`` with a JSON-null value is admitted."""
    session_id = "76666666-6666-4666-8666-666666666666"
    _install_owned_transcript(
        tmp_path,
        monkeypatch,
        _story_planner_transcript_with_usage_extra(session_id, key, value),
        session_id,
    )
    with pytest.raises(usage_transcript.TranscriptError, match="usage is invalid"):
        _parse_story_planner_transcript(tmp_path, session_id)
