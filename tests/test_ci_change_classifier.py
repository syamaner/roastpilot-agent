"""Behavioural and structural tests for the inert docs-only CI classifier."""

from __future__ import annotations

import ast
import io
import runpy
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import ci_change_classifier as classifier
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_BASE = "a" * 40
_HEAD = "b" * 40
_MERGE_BASE = "c" * 40


class _FakeGitProcess:
    """Small byte-streaming local Git process double for subprocess-bound tests."""

    def __init__(self, output: bytes = b"", *, running: bool = False) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode: int | None = None if running else 0

    def poll(self) -> int | None:
        """Return the configured child state."""

        return self.returncode

    def terminate(self) -> None:
        """Make the fake child exit after termination."""

        self.returncode = -15

    def kill(self) -> None:
        """Make the fake child exit after a forced kill."""

        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """Return the configured child exit state without blocking."""

        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("git", 0.0)
        return self.returncode


class _LiveGitProcess(_FakeGitProcess):
    """A process double that releases output only after live polling begins."""

    def __init__(self, output: bytes | tuple[bytes, ...]) -> None:
        super().__init__(running=True)
        self.stdout = _LiveOutput(output)
        self.polls = 0
        self.terminated = False
        self.killed = False
        self.waits = 0

    def poll(self) -> int | None:
        """Remain live and release stdout after the second poll."""

        self.polls += 1
        if self.polls == 2:
            self.stdout.release.set()
        return self.returncode

    def terminate(self) -> None:
        """Record termination and unblock the stream."""

        self.terminated = True
        self.stdout.release.set()
        super().terminate()

    def kill(self) -> None:
        """Record forced cleanup."""

        self.killed = True
        super().kill()

    def wait(self, timeout: float | None = None) -> int:
        """Count bounded waits before using the ordinary fake result."""

        del timeout
        self.waits += 1
        return super().wait()


class _LiveOutput:
    """One or more chunks that cannot be read before polling releases them."""

    def __init__(self, output: bytes | tuple[bytes, ...]) -> None:
        self.chunks = (output,) if isinstance(output, bytes) else output
        self.release = threading.Event()
        self.reads = 0

    def read(self, _size: int) -> bytes:
        """Block until the process has been polled twice, then return EOF."""

        self.release.wait()
        self.reads += 1
        return self.chunks[self.reads - 1] if self.reads <= len(self.chunks) else b""


class _FailingLiveOutput(_LiveOutput):
    """A live stream that returns one valid prefix before its next read fails."""

    def __init__(self, output: bytes | tuple[bytes, ...]) -> None:
        """Initialize the blocked stream and its deterministic failure signal."""

        super().__init__(output)
        self.failed = threading.Event()

    def read(self, size: int) -> bytes:
        """Return the configured prefix once, then model an OS-level drain failure."""

        result = super().read(size)
        if self.reads > len(self.chunks):
            self.failed.set()
            raise OSError("stdout drain failed")
        return result


class _Clock:
    """Monotonic deterministic clock without exhausted iterator behaviour."""

    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        """Return the current time and advance by a fixed amount."""

        value = self.value
        self.value += self.step
        return value


def _name_status(*fields: bytes) -> bytes:
    """Build a NUL-delimited Git ``--name-status`` payload for one test."""

    return b"\0".join(fields) + b"\0"


def _regular_tree_entry(path: bytes, mode: bytes = b"100644") -> bytes:
    """Build a regular-file ``git ls-tree -z`` record for ``path``."""

    return mode + b" blob " + (b"d" * 40) + b"\t" + path + b"\0"


def _install_git_fixture(
    monkeypatch: pytest.MonkeyPatch,
    diff: bytes,
    *,
    regular_paths: set[tuple[str, bytes]] | None = None,
    modes: dict[tuple[str, bytes], bytes] | None = None,
) -> list[tuple[str, ...]]:
    """Install a deterministic local Git transcript and return its command log."""

    calls: list[tuple[str, ...]] = []
    admitted = regular_paths

    def fake_git(arguments: Iterable[str], *, deadline: float | None = None) -> bytes:
        del deadline
        command = tuple(arguments)
        calls.append(command)
        if command[:2] == ("cat-file", "-e"):
            return b""
        if command[:1] == ("merge-base",):
            return f"{_MERGE_BASE}\n".encode()
        if command[:2] == ("diff", "-z"):
            return diff
        if command[:2] == ("ls-tree", "-z"):
            commit, path = command[2], command[-1].encode()
            if admitted is None or (commit, path) in admitted:
                mode = b"100644" if modes is None else modes.get((commit, path), b"100644")
                return _regular_tree_entry(path, mode)
            return b"120000 blob " + (b"e" * 40) + b"\t" + path + b"\0"
        raise AssertionError(f"unexpected Git command: {command!r}")

    monkeypatch.setattr(classifier, "_run_git", fake_git)
    return calls


def test_run_git_uses_only_the_local_closed_command_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess wrapper uses no shell, network, or inherited rendered path list."""

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(arguments: list[str], **kwargs: object) -> _FakeGitProcess:
        calls.append((arguments, kwargs))
        return _FakeGitProcess(b"local-output")

    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    assert classifier._run_git(["merge-base", _BASE, _HEAD]) == b"local-output"  # pyright: ignore[reportPrivateUsage]
    assert calls == [
        (
            ["git", "merge-base", _BASE, _HEAD],
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "shell": False,
            },
        )
    ]


@pytest.mark.docs_ci
def test_run_git_caps_a_near_deadline_call_to_the_remaining_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started Git call cannot outlive the classifier's remaining total budget."""

    calls: list[list[str]] = []

    def fake_popen(arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        calls.append(arguments)
        return _FakeGitProcess()

    monkeypatch.setattr(classifier.time, "monotonic", lambda: 59.5)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    classifier._run_git(["status"], deadline=60.0)  # pyright: ignore[reportPrivateUsage]
    assert calls == [["git", "status"]]


@pytest.mark.docs_ci
def test_run_git_does_not_spawn_when_the_total_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero remaining budget fails closed before any subprocess launch."""

    monkeypatch.setattr(classifier.time, "monotonic", lambda: 60.0)

    def no_spawn(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        pytest.fail("expired budget must not spawn Git")

    monkeypatch.setattr(classifier.subprocess, "run", no_spawn)
    with pytest.raises(classifier._BudgetExceeded):  # pyright: ignore[reportPrivateUsage]
        classifier._run_git(["status"], deadline=60.0)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_run_git_terminates_on_mid_run_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live polling notices streamed output over the cap and terminates the child."""

    process = _LiveGitProcess(b"five!")

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(classifier, "_MAX_GIT_OUTPUT_BYTES", 4)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(classifier.time, "sleep", no_sleep)
    with pytest.raises(classifier._GitOutputLimitExceeded):  # pyright: ignore[reportPrivateUsage]
        classifier._run_git(["status"])  # pyright: ignore[reportPrivateUsage]
    assert process.polls >= 2
    assert process.terminated


@pytest.mark.docs_ci
def test_run_git_discards_later_live_chunks_after_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain keeps boundedly consuming chunks after output becomes untrusted."""
    process = _LiveGitProcess((b"five!", b"discarded"))

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(classifier, "_MAX_GIT_OUTPUT_BYTES", 4)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(classifier.time, "sleep", no_sleep)

    with pytest.raises(classifier._GitOutputLimitExceeded):  # pyright: ignore[reportPrivateUsage]
        classifier._run_git(["status"])  # pyright: ignore[reportPrivateUsage]

    assert process.stdout.reads == 3
    assert process.terminated


@pytest.mark.docs_ci
def test_run_git_terminates_on_mid_run_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A running child is terminated when the total budget expires during polling."""

    process = _LiveGitProcess(b"")
    clock = _Clock(1.0)

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(classifier.time, "monotonic", clock)
    monkeypatch.setattr(classifier.time, "sleep", no_sleep)
    with pytest.raises(classifier._BudgetExceeded):  # pyright: ignore[reportPrivateUsage]
        classifier._run_git(["status"], deadline=2.5)  # pyright: ignore[reportPrivateUsage]
    assert process.terminated


@pytest.mark.docs_ci
def test_run_git_terminates_on_mid_run_per_call_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A running child is terminated when its per-call interval expires."""

    process = _LiveGitProcess(b"")
    clock = _Clock(1.0)

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(classifier, "_GIT_CALL_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(classifier.time, "monotonic", clock)
    monkeypatch.setattr(classifier.time, "sleep", no_sleep)
    with pytest.raises(subprocess.TimeoutExpired):
        classifier._run_git(["status"])  # pyright: ignore[reportPrivateUsage]
    assert process.terminated


@pytest.mark.docs_ci
def test_terminate_git_process_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup kills a child when its bounded post-terminate wait expires."""

    process = _LiveGitProcess(b"")
    original_wait = process.wait

    def timeout_once(timeout: float | None = None) -> int:
        """Force the first cleanup wait to time out, then allow the kill wait."""

        if process.waits == 0:
            process.waits += 1
            raise subprocess.TimeoutExpired("git", timeout or 0.0)
        return original_wait(timeout)

    monkeypatch.setattr(process, "wait", timeout_once)
    classifier._terminate_git_process(cast(subprocess.Popen[bytes], process))  # pyright: ignore[reportPrivateUsage]
    assert process.terminated and process.killed


@pytest.mark.docs_ci
def test_run_git_raises_for_a_completed_nonzero_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drained live process with a nonzero result cannot yield trusted Git output."""
    process = _LiveGitProcess(b"")
    process.returncode = 7

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)

    with pytest.raises(subprocess.CalledProcessError) as error:
        classifier._run_git(["status"])  # pyright: ignore[reportPrivateUsage]

    assert error.value.returncode == 7
    assert process.polls >= 2


@pytest.mark.docs_ci
def test_run_git_drain_failure_after_docs_prefix_makes_classification_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain error after a plausible docs prefix cannot yield a trusted classification."""

    output = _name_status(b"A", b"docs/guide.md")
    process = _LiveGitProcess(output)
    process.stdout = _FailingLiveOutput(output)
    regular_file = _FakeGitProcess(b"100644 blob " + b"0" * 40 + b"\tdocs/guide.md\0")
    processes = [process, regular_file]

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        return processes.pop(0)

    def complete_after_release() -> int | None:
        process.polls += 1
        if process.polls == 1:
            process.stdout.release.set()
            return None
        process.returncode = 0
        return 0

    monkeypatch.setattr(process, "poll", complete_after_release)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    assert process.stdout.reads == 2


@pytest.mark.docs_ci
def test_run_git_reraises_a_drain_failure_after_a_valid_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller cannot consume a partially drained, otherwise valid Git payload."""

    process = _LiveGitProcess(_name_status(b"A", b"docs/guide.md"))
    process.stdout = _FailingLiveOutput(_name_status(b"A", b"docs/guide.md"))
    process.returncode = 0

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError, match="stdout drain failed"):
        classifier._run_git(["diff", "--name-status"])  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_run_git_terminates_a_live_process_when_its_drain_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live polling iteration observes drain failure before accepting any output."""

    process = _LiveGitProcess(b"docs/guide.md")
    output = _FailingLiveOutput(b"docs/guide.md")
    process.stdout = output

    def remain_live_until_failure() -> None:
        """Release the drain and keep the child live until its error is visible."""

        process.polls += 1
        output.release.set()
        assert output.failed.wait(timeout=1.0)
        return None

    def fake_popen(_arguments: list[str], **_kwargs: object) -> _LiveGitProcess:
        return process

    monkeypatch.setattr(process, "poll", remain_live_until_failure)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(classifier, "_GIT_CALL_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(OSError, match="stdout drain failed"):
        classifier._run_git(["status"])  # pyright: ignore[reportPrivateUsage]
    assert process.terminated


@pytest.mark.parametrize("status", [b"A", b"M", b"D"])
def test_classifies_single_allowed_markdown_status_as_docs_only(
    monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    """A, M, and D regular nested Markdown paths are the closed positive set."""

    _install_git_fixture(monkeypatch, _name_status(status, b"docs/nested/change.md"))
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )


def test_classifies_multiple_nested_allowed_markdown_paths_as_docs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty mixed A/M/D Markdown-only set remains docs-only."""

    _install_git_fixture(
        monkeypatch,
        _name_status(
            b"A",
            b"docs/guide/first.md",
            b"M",
            b"docs/guide/nested/second.md",
            b"D",
            b"docs/old.md",
        ),
    )
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )


@pytest.mark.parametrize("status", [b"R100", b"C100"])
def test_classifies_allowed_rename_and_copy_as_docs_only(
    monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    """Rename and copy need both sides inside the closed Markdown grammar."""

    _install_git_fixture(monkeypatch, _name_status(status, b"docs/old.md", b"docs/new.md"))
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )


@pytest.mark.parametrize(
    "fields",
    [
        (b"R100", b"docs/old.md", b"src/new.py"),
        (b"C100", b"src/old.py", b"docs/new.md"),
        (b"A", b"README.md"),
        (b"A", b".github/workflows/ci.yml"),
        (b"A", b".claude/role.md"),
        (b"A", b".codex/config.md"),
        (b"A", b".agents/control.md"),
        (b"A", b"AGENTS.md"),
        (b"A", b"docs/guide.txt"),
        (b"A", b"docs/README.MD"),
        (b"A", b"docs/../escape.md"),
        (b"A", b"docs/./dot.md"),
        (b"A", b"tests/test_example.py"),
        (b"A", b"scripts/tool.py"),
        (b"A", b"pyproject.toml"),
        (b"A", b"uv.lock"),
        (b"A", b"tests/fixtures/contract/example.md"),
        (b"A", b"web/src/page.tsx"),
        (b"A", b"codecov.yml"),
        (b"A", b"docs/unsafe\nmode=docs-only.md"),
        (b"A", b"docs/unsafe=mode.md"),
        (b"A", b"docs/invalid\xff.md"),
    ],
)
def test_boundary_crossing_or_unusual_path_is_full(
    monkeypatch: pytest.MonkeyPatch, fields: tuple[bytes, ...]
) -> None:
    """Every path outside the exact case-sensitive grammar fails closed."""

    _install_git_fixture(monkeypatch, _name_status(*fields))
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


@pytest.mark.parametrize("status", [b"T", b"U", b"X", b"R10", b"R1000"])
def test_unrecognised_or_non_regular_status_is_full(
    monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    """Type changes, unmerged records, and malformed similarity statuses fail closed."""

    fields = (status, b"docs/safe.md")
    _install_git_fixture(monkeypatch, _name_status(*fields))
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


def test_symlink_is_full_even_when_its_path_matches_the_docs_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git object mode confirmation rejects a docs-path symlink."""

    _install_git_fixture(monkeypatch, _name_status(b"A", b"docs/link.md"), regular_paths=set())
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


@pytest.mark.parametrize("status", [b"M", b"R100", b"C100"])
def test_regular_mode_transition_is_full(monkeypatch: pytest.MonkeyPatch, status: bytes) -> None:
    """An admitted docs path changing 100644/100755 fails closed."""

    paths = (b"docs/safe.md",) if status == b"M" else (b"docs/old.md", b"docs/new.md")
    modes = {(_MERGE_BASE, paths[0]): b"100644", (_HEAD, paths[-1]): b"100755"}
    _install_git_fixture(monkeypatch, _name_status(status, *paths), modes=modes)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


@pytest.mark.parametrize("status", [b"M", b"R100", b"C100"])
def test_unchanged_regular_mode_is_docs_only(
    monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    """Content-only changes retain docs-only when both regular modes match."""

    paths = (b"docs/safe.md",) if status == b"M" else (b"docs/old.md", b"docs/new.md")
    modes = {(_MERGE_BASE, paths[0]): b"100755", (_HEAD, paths[-1]): b"100755"}
    _install_git_fixture(monkeypatch, _name_status(status, *paths), modes=modes)
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )


@pytest.mark.parametrize("status", [b"A", b"D"])
def test_executable_regular_addition_or_deletion_is_docs_only(
    monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    """A/D admit either regular mode because no mode comparison exists."""

    commit = _HEAD if status == b"A" else _MERGE_BASE
    modes = {(commit, b"docs/executable.md"): b"100755"}
    _install_git_fixture(monkeypatch, _name_status(status, b"docs/executable.md"), modes=modes)
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )


@pytest.mark.parametrize(
    "payload",
    [b"\xff\0docs/safe.md\0", b"A\0", b"R100\0docs/old.md\0", b"A\0docs/safe.md"],
)
def test_malformed_nul_status_records_are_rejected(payload: bytes) -> None:
    """Truncated, non-ASCII, and unterminated name-status records are unknown."""

    assert classifier._parse_name_status(payload) is None  # pyright: ignore[reportPrivateUsage]


def test_regular_file_confirmation_rejects_an_unexpected_tree_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path mismatch in the local tree response cannot be treated as regular."""

    def unexpected_tree(_arguments: Sequence[str], *, deadline: float | None = None) -> bytes:
        del deadline
        return b"100644 blob deadbeef\tother.md\0"

    monkeypatch.setattr(classifier, "_run_git", unexpected_tree)
    assert not classifier._is_regular_file(_HEAD, b"docs/safe.md")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "entry",
    [
        ("A", (b"docs/safe.md",)),
        ("M", (b"docs/safe.md",)),
        ("D", (b"docs/safe.md",)),
        ("R100", (b"docs/old.md", b"docs/new.md")),
    ],
)
def test_regular_file_confirmation_failure_is_full_for_every_admitted_status(
    monkeypatch: pytest.MonkeyPatch, entry: tuple[str, tuple[bytes, ...]]
) -> None:
    """No admitted status can bypass the regular-file confirmation."""

    def not_regular(_commit: str, _path: bytes, *, deadline: float | None = None) -> bytes | None:
        del deadline
        return None

    monkeypatch.setattr(classifier, "_regular_file_mode", not_regular)
    assert not classifier._entries_are_docs_only([entry], _MERGE_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


def test_empty_entries_and_missing_output_path_are_safe_noops() -> None:
    """Internal empty data and absent GitHub output both retain the full-safe boundary."""

    assert not classifier._entries_are_docs_only([], _MERGE_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]
    classifier._write_output(classifier.ChangeMode.FULL, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("event_name", "base_sha", "head_sha"),
    [
        ("push", _BASE, _HEAD),
        ("pull_request", "", _HEAD),
        ("pull_request", _BASE.upper(), _HEAD),
        ("pull_request", _BASE[:-1], _HEAD),
    ],
)
def test_non_pr_or_malformed_inputs_are_full_without_git(
    monkeypatch: pytest.MonkeyPatch, event_name: str, base_sha: str, head_sha: str
) -> None:
    """Only exact pull-request SHA inputs may reach local Git."""

    calls = _install_git_fixture(monkeypatch, _name_status(b"M", b"docs/safe.md"))
    assert classifier.classify_change(event_name, base_sha, head_sha) is classifier.ChangeMode.FULL
    assert calls == []


@pytest.mark.parametrize("failure", [b"", b"not-a-sha\n"])
def test_empty_or_invalid_merge_base_is_full(
    monkeypatch: pytest.MonkeyPatch, failure: bytes
) -> None:
    """An empty or malformed merge-base result cannot be docs-only."""

    def fake_git(arguments: Iterable[str], *, deadline: float | None = None) -> bytes:
        del deadline
        command = tuple(arguments)
        if command[:2] == ("cat-file", "-e"):
            return b""
        if command[:1] == ("merge-base",):
            return failure
        raise AssertionError(command)

    monkeypatch.setattr(classifier, "_run_git", fake_git)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


def test_empty_comparison_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful but empty merge-base comparison cannot be docs-only."""

    _install_git_fixture(monkeypatch, b"")
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


@pytest.mark.parametrize("failing_command", ["cat-file", "merge-base", "diff", "ls-tree"])
def test_git_failures_and_exceptions_are_full(
    monkeypatch: pytest.MonkeyPatch, failing_command: str
) -> None:
    """Missing objects, diff failure, and any Git exception are fail-closed."""

    def fake_git(arguments: Iterable[str], *, deadline: float | None = None) -> bytes:
        del deadline
        command = tuple(arguments)
        if command[0] == failing_command:
            raise OSError("unavailable")
        if command[:2] == ("cat-file", "-e"):
            return b""
        if command[:1] == ("merge-base",):
            return f"{_MERGE_BASE}\n".encode()
        if command[:2] == ("diff", "-z"):
            return _name_status(b"A", b"docs/safe.md")
        if command[:2] == ("ls-tree", "-z"):
            return _regular_tree_entry(command[-1].encode())
        raise AssertionError(command)

    monkeypatch.setattr(classifier, "_run_git", fake_git)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


def test_uses_merge_base_and_exact_nul_name_status_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison is merge-base-to-head, never a two-dot or rendered path list."""

    calls = _install_git_fixture(monkeypatch, _name_status(b"M", b"docs/safe.md"))
    assert (
        classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.DOCS_ONLY
    )
    assert ("merge-base", _BASE, _HEAD) in calls
    assert (
        "diff",
        "-z",
        "--name-status",
        "--find-renames",
        "--no-color",
        _MERGE_BASE,
        _HEAD,
    ) in calls
    assert ("ls-tree", "-z", _MERGE_BASE, "--", "docs/safe.md") in calls


# ---------------------------------------------------------------------------
# Bounded classifier worktime (D180 §2.7/§3.3, B4-ii)
# ---------------------------------------------------------------------------


def test_worktime_constants_are_module_level_literals_not_environment_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR cannot widen its own classifier budget via the environment."""

    assert classifier._GIT_CALL_TIMEOUT_SECONDS == 20.0  # pyright: ignore[reportPrivateUsage]
    assert classifier._TOTAL_BUDGET_SECONDS == 60.0  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("_GIT_CALL_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("_TOTAL_BUDGET_SECONDS", "0")
    assert classifier._GIT_CALL_TIMEOUT_SECONDS == 20.0  # pyright: ignore[reportPrivateUsage]
    assert classifier._TOTAL_BUDGET_SECONDS == 60.0  # pyright: ignore[reportPrivateUsage]


def test_a_slow_git_call_past_the_per_call_timeout_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live Popen polling loop turns a per-call expiry into ``FULL``.

    The deterministic clock stays below the total deadline while advancing
    beyond the per-call interval, so iterator exhaustion cannot reach the
    classifier's broad exception boundary instead of the timeout guard.
    """

    calls: list[list[str]] = []

    def fake_popen(arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        calls.append(arguments)
        return _FakeGitProcess(running=True)

    clock = _Clock(11.0)
    monkeypatch.setattr(classifier.time, "monotonic", clock)

    def no_sleep(_seconds: float) -> None:
        """Avoid a real delay while exercising the bounded timeout path."""

    monkeypatch.setattr(classifier.time, "sleep", no_sleep)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    assert calls == [["git", "cat-file", "-e", f"{_BASE}^{{commit}}"]]
    assert 22.0 <= clock.value < classifier._TOTAL_BUDGET_SECONDS  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_run_git_accepts_exactly_the_closed_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Git result at the byte cap remains available to the closed parser."""

    monkeypatch.setattr(classifier, "_MAX_GIT_OUTPUT_BYTES", 4)

    def fake_popen_exact(_arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        """Return a child whose output is exactly the small test cap."""

        return _FakeGitProcess(b"four")

    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen_exact)
    assert classifier._run_git(["status"]) == b"four"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_excess_git_output_is_full_and_stops_the_git_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One byte beyond the cap fails closed before any later Git call can run."""

    calls: list[list[str]] = []

    def fake_popen(arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        calls.append(arguments)
        return _FakeGitProcess(b"five!")

    monkeypatch.setattr(classifier, "_MAX_GIT_OUTPUT_BYTES", 4)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    assert calls == [["git", "cat-file", "-e", f"{_BASE}^{{commit}}"]]


def test_exceeded_total_budget_is_full_and_issues_no_further_git_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An advanced clock past the total budget stops before any further Git call.

    This exercises the real ``_run_git``/``classify_change`` deadline check
    (only ``subprocess.run`` and ``time.monotonic`` are replaced) so an M19
    mutation deleting the monotonic deadline check is actually caught: without
    it, the second Git call would proceed and this test's call-count
    assertion would fail.
    """

    calls: list[list[str]] = []
    # 1st monotonic() call establishes the deadline (60s from "0.0"); the 2nd
    # is the real _run_git deadline check before the first Git call (still
    # under budget); every later call (the check before the SECOND Git call)
    # returns a value already past the deadline.
    responses = iter([0.0, 1.0])

    def fake_monotonic() -> float:
        try:
            return next(responses)
        except StopIteration:
            return 1_000.0

    def fake_popen(arguments: list[str], **_kwargs: object) -> _FakeGitProcess:
        calls.append(arguments)
        return _FakeGitProcess()

    monkeypatch.setattr(classifier.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(classifier.subprocess, "Popen", fake_popen)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    # Only the first `cat-file` call ran; the deadline check before the second
    # `cat-file` call raised _BudgetExceeded, so no further Git call happened.
    assert len(calls) == 1


def test_directly_raised_timeout_expired_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``TimeoutExpired`` raised from anywhere in the Git transcript is FULL."""

    def fake_git(arguments: Iterable[str], *, deadline: float | None = None) -> bytes:
        del deadline
        raise subprocess.TimeoutExpired(cmd=list(arguments), timeout=20.0)

    monkeypatch.setattr(classifier, "_run_git", fake_git)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


def test_run_git_rejects_a_call_already_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_git`` itself refuses to spawn a process once the deadline has passed."""

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("no subprocess should be spawned past the deadline")

    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
    with pytest.raises(classifier._BudgetExceeded):  # pyright: ignore[reportPrivateUsage]
        classifier._run_git(["merge-base", _BASE, _HEAD], deadline=time.monotonic() - 1)  # pyright: ignore[reportPrivateUsage]


def test_main_writes_one_closed_output_line_without_raw_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Workflow output has exactly the two-token closed grammar and no path data."""

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _install_git_fixture(monkeypatch, _name_status(b"A", b"docs/safe.md"))
    assert (
        classifier.main(["--event-name", "pull_request", "--base-sha", _BASE, "--head-sha", _HEAD])
        == 0
    )
    assert output.read_text(encoding="utf-8") == "mode=docs-only\n"
    assert "docs/safe.md" not in output.read_text(encoding="utf-8")


def test_main_converts_an_unexpected_exception_to_full_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The command boundary never raises an unexpected classifier exception."""

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def explode(*_args: object) -> classifier.ChangeMode:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(classifier, "classify_change", explode)
    assert (
        classifier.main(["--event-name", "pull_request", "--base-sha", _BASE, "--head-sha", _HEAD])
        == 0
    )
    assert output.read_text(encoding="utf-8") == "mode=full\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--event-name", "pull_request", "--head-sha", _HEAD],
        ["--event-name", "pull_request", "--base-sha", _BASE],
    ],
)
def test_main_converts_missing_required_sha_to_full_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> None:
    """Missing required SHA flags exit cleanly after emitting the full-safe mode."""

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert classifier.main(arguments) == 0
    assert output.read_text(encoding="utf-8") == "mode=full\n"


def test_main_suppresses_a_secondary_output_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable output sink cannot raise through the workflow boundary."""

    def explode(*_args: object) -> classifier.ChangeMode:
        raise RuntimeError("unexpected")

    def fail_output(*_args: object) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(classifier, "classify_change", explode)
    monkeypatch.setattr(classifier, "_write_output", fail_output)
    assert (
        classifier.main(["--event-name", "pull_request", "--base-sha", _BASE, "--head-sha", _HEAD])
        == 0
    )


def test_module_entrypoint_exits_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed script entrypoint keeps its failure-safe zero exit boundary."""

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci_change_classifier.py",
            "--event-name",
            "push",
            "--base-sha",
            _BASE,
            "--head-sha",
            _HEAD,
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(_REPO / "scripts" / "ci_change_classifier.py"), run_name="__main__")
    assert result.value.code == 0
    assert output.read_text(encoding="utf-8") == "mode=full\n"


_READ_METHOD_NAMES = frozenset({"read_text", "read_bytes", "open"})
_GLOB_METHOD_NAMES = frozenset({"glob", "rglob"})
_MAX_DOCS_CALL_GRAPH_DEPTH = 8


def _string_constants(expression: ast.expr) -> list[str]:
    """Return every folded string literal within an expression subtree."""

    return [
        node.value
        for node in ast.walk(expression)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _expression_is_docs_markdown(expression: ast.expr, aliases: set[str]) -> bool:
    """Return whether an expression is provably rooted at ``docs/**/*.md``.

    A known docs-rooted alias always counts. Otherwise an expression counts
    when its folded string constants include both a ``docs``/``docs/``
    segment and a ``.md``-suffixed segment, wherever in the subtree they
    fold from — this generically covers ``Path(x) / "docs" / name``,
    ``.joinpath("docs", ...)``, ``os.path.join("docs", stem + ".md")``, and
    f-string/``str.format`` construction alike, because each folds its
    literal segments into ``Constant`` nodes regardless of concatenation
    style.
    """

    if isinstance(expression, ast.Name):
        return expression.id in aliases
    strings = _string_constants(expression)
    return any(value == "docs" or value.startswith("docs/") for value in strings) and any(
        value.endswith(".md") for value in strings
    )


def _expression_has_docs_root(expression: ast.expr) -> bool:
    """Return whether an expression contains the literal ``docs`` directory component."""

    return any(
        isinstance(node, ast.Constant) and node.value == "docs" for node in ast.walk(expression)
    )


def _expression_uses_alias(expression: ast.expr, aliases: set[str]) -> bool:
    """Return whether an expression refers to one of the known path aliases."""

    return any(isinstance(node, ast.Name) and node.id in aliases for node in ast.walk(expression))


def _expression_completes_partial_docs_alias(expression: ast.expr, aliases: set[str]) -> bool:
    """Return whether path composition completes a docs-root alias with ``.md``."""

    return _expression_uses_alias(expression, aliases) and any(
        value.endswith(".md") for value in _string_constants(expression)
    )


def _is_docs_markdown_glob_call(node: ast.AST, root_aliases: set[str]) -> bool:
    """Return whether a call is the closed docs ``rglob('*.md')`` shape.

    ``root_aliases`` names any variable already known to carry a docs-rooted
    directory (full or partial evidence alike — a directory root never itself
    ends in ``.md``, so partial evidence of the ``docs`` component is already
    conclusive proof for a glob root).
    """

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "iterdir"
        and not node.args
        and not node.keywords
    ):
        root = node.func.value
        return (
            isinstance(root, ast.Name) and root.id in root_aliases
        ) or _expression_has_docs_root(root)
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rglob"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "*.md"
    ):
        return False
    root = node.func.value
    if isinstance(root, ast.Name):
        return root.id in root_aliases
    return _expression_has_docs_root(root)


def _is_docs_rooted_glob_call(node: ast.AST, root_aliases: set[str]) -> bool:
    """Return whether a glob-like call has static docs-root evidence."""

    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _GLOB_METHOD_NAMES | {"iterdir"}
    ):
        return False
    root = node.func.value
    return (isinstance(root, ast.Name) and root.id in root_aliases) or _expression_has_docs_root(
        root
    )


def _builtin_open_target(node: ast.Call) -> ast.expr:
    """Return an ``open`` call's sole file receiver or fail closed on shape drift."""

    file_keywords = [keyword for keyword in node.keywords if keyword.arg == "file"]
    if any(keyword.arg is None for keyword in node.keywords):
        raise AssertionError("builtin open has dynamic keyword expansion")
    if len(file_keywords) > 1:
        raise AssertionError("builtin open has duplicate file keywords")
    if node.args and file_keywords:
        raise AssertionError("builtin open has conflicting positional and file keyword receivers")
    if not node.args and not file_keywords:
        raise AssertionError("builtin open has no statically unique file receiver")
    if node.args and isinstance(node.args[0], ast.Starred):
        raise AssertionError("builtin open has dynamic positional receiver")
    return node.args[0] if node.args else file_keywords[0].value


def _qualified_open_target(node: ast.Call) -> ast.expr | None:
    """Return an exact ``io.open``/``builtins.open`` receiver, if present.

    Only the literal standard-library module spellings are admitted here.  Import
    aliases are rejected separately before the call graph is traversed, so a
    renamed qualified opener cannot silently evade the docs-reader marker rule.
    """

    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"io", "builtins"}
    ):
        return None
    return _builtin_open_target(node)


_SUBPROCESS_ENTRY_POINTS = frozenset({"Popen", "call", "check_call", "check_output", "run"})


def _subprocess_call_aliases(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Return exact and rebound aliases for admitted subprocess entry points."""

    modules = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == "subprocess"
    }
    calls = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "subprocess"
        for alias in statement.names
        if alias.name in _SUBPROCESS_ENTRY_POINTS
    }
    rebound = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name) and target.id in modules | calls
    }
    return modules - rebound, calls - rebound, rebound


def _subprocess_docs_call_state(
    node: ast.Call,
    module_aliases: set[str],
    call_aliases: set[str],
    rebound: set[str],
    command_docs_aliases: set[str] | None = None,
    command_partial_aliases: set[str] | None = None,
    command_ambiguous_aliases: set[str] | None = None,
    command_non_docs_aliases: set[str] | None = None,
) -> str:
    """Return ``docs``, ``ambiguous``, or ``none`` for one admitted subprocess call."""

    target_is_subprocess = (isinstance(node.func, ast.Name) and node.func.id in call_aliases) or (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in _SUBPROCESS_ENTRY_POINTS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in module_aliases
    )
    target_is_rebound = (isinstance(node.func, ast.Name) and node.func.id in rebound) or (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in rebound
        and node.func.attr in _SUBPROCESS_ENTRY_POINTS
    )
    if target_is_rebound:
        return "ambiguous"
    if not target_is_subprocess:
        return "none"
    if any(keyword.arg is None for keyword in node.keywords):
        return "ambiguous"
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        return "ambiguous"
    arguments = (
        [node.args[0]]
        if node.args
        else [keyword.value for keyword in node.keywords if keyword.arg == "args"]
    )
    if any(
        isinstance(argument, ast.Name) and argument.id in (command_ambiguous_aliases or set())
        for argument in arguments
    ):
        return "ambiguous"
    if any(
        isinstance(argument, ast.Name) and argument.id in (command_docs_aliases or set())
        for argument in arguments
    ):
        return "docs"
    if any(
        isinstance(argument, ast.Name) and argument.id in (command_partial_aliases or set())
        for argument in arguments
    ):
        return "ambiguous"
    if any(
        isinstance(argument, ast.Name) and argument.id in (command_non_docs_aliases or set())
        for argument in arguments
    ):
        return "none"
    if any(
        not isinstance(argument, (ast.Constant, ast.List, ast.Tuple, ast.Name))
        for argument in arguments
    ):
        return "ambiguous"
    strings = [value for argument in arguments for value in _string_constants(argument)]
    if any(value.startswith("docs/") and value.endswith(".md") for value in strings):
        return "docs"
    if any(value == "docs" or value.startswith("docs/") for value in strings):
        return "ambiguous"
    if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    ):
        return "ambiguous"
    return "none"


def _assert_no_aliased_qualified_open_calls(tree: ast.Module, filename: str) -> None:
    """Reject invoked module aliases that cannot retain qualified-open provenance."""

    module_aliases: set[str] = set()
    open_aliases: set[str] = set()
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name in {"io", "builtins"} and alias.asname is not None:
                    module_aliases.add(alias.asname)
        elif isinstance(statement, ast.ImportFrom) and statement.module == "io":
            for alias in statement.names:
                if alias.name == "open":
                    open_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "open"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
        ):
            raise AssertionError(f"{filename}:{node.lineno}: aliased qualified open is ambiguous")
        if isinstance(node.func, ast.Name) and node.func.id in open_aliases:
            raise AssertionError(f"{filename}:{node.lineno}: imported open alias is ambiguous")


def _builtin_open_import_aliases(tree: ast.Module) -> set[str]:
    """Return direct ``builtins.open`` bindings that retain builtin provenance."""

    return {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "builtins"
        for alias in statement.names
        if alias.name == "open"
    }


def _pytest_fixture_aliases(tree: ast.Module) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return fixture and pytest-module aliases, separating rebound provenance."""

    aliases = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "pytest"
        for alias in statement.names
        if alias.name == "fixture"
    }
    rebound = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name) and target.id in aliases
    }
    module_aliases = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == "pytest"
    }
    module_rebound = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name) and target.id in module_aliases
    }
    return aliases - rebound, rebound, module_aliases - module_rebound, module_rebound


def _is_pytest_fixture_decorator(
    decorator: ast.expr,
    fixture_aliases: set[str],
    ambiguous_fixture_aliases: set[str],
    pytest_module_aliases: set[str],
    ambiguous_pytest_module_aliases: set[str],
) -> bool:
    """Return whether a decorator expression is (a call to) ``pytest.fixture``."""

    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        if not isinstance(target.value, ast.Name):
            return False
        if target.value.id in ambiguous_pytest_module_aliases:
            raise AssertionError(f"pytest module alias `{target.value.id}` is ambiguous")
        return target.attr == "fixture" and target.value.id in pytest_module_aliases | {"pytest"}
    if isinstance(target, ast.Name):
        if target.id in ambiguous_fixture_aliases:
            raise AssertionError(f"fixture decorator alias `{target.id}` is ambiguous")
        return target.id in fixture_aliases
    return False


def _fixture_exposed_name(
    function_name: str,
    decorators: list[ast.expr],
    fixture_aliases: set[str],
    ambiguous_fixture_aliases: set[str],
    pytest_module_aliases: set[str],
    ambiguous_pytest_module_aliases: set[str],
) -> str | None:
    """Return one statically resolved fixture name or fail closed on ambiguity."""

    fixture_decorators = [
        decorator
        for decorator in decorators
        if _is_pytest_fixture_decorator(
            decorator,
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        )
    ]
    if not fixture_decorators:
        return None
    if len(fixture_decorators) != 1:
        raise AssertionError(
            f"fixture `{function_name}` has multiple fixture decorators; cannot resolve its name"
        )
    decorator = fixture_decorators[0]
    if not isinstance(decorator, ast.Call):
        return function_name
    if decorator.args or any(keyword.arg is None for keyword in decorator.keywords):
        raise AssertionError(f"fixture `{function_name}` has an ambiguous fixture decorator shape")
    name_keywords = [keyword for keyword in decorator.keywords if keyword.arg == "name"]
    if not name_keywords:
        return function_name
    if len(name_keywords) != 1:
        raise AssertionError(f"fixture `{function_name}` has conflicting `name=` overrides")
    name_value = name_keywords[0].value
    if not isinstance(name_value, ast.Constant) or not isinstance(name_value.value, str):
        raise AssertionError(f"fixture `{function_name}` has a non-literal `name=` override")
    if not name_value.value:
        raise AssertionError(f"fixture `{function_name}` has an empty `name=` override")
    return name_value.value


def _fixture_is_autouse(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    fixture_aliases: set[str],
    ambiguous_fixture_aliases: set[str],
    pytest_module_aliases: set[str],
    ambiguous_pytest_module_aliases: set[str],
) -> bool:
    """Return a fixture's literal autouse setting or fail closed on ambiguity."""

    decorators = [
        decorator
        for decorator in node.decorator_list
        if _is_pytest_fixture_decorator(
            decorator,
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        )
    ]
    if not decorators or not isinstance(decorators[0], ast.Call):
        return False
    values = [keyword.value for keyword in decorators[0].keywords if keyword.arg == "autouse"]
    if not values:
        return False
    if (
        len(values) != 1
        or not isinstance(values[0], ast.Constant)
        or not isinstance(values[0].value, bool)
    ):
        raise AssertionError(f"fixture `{node.name}` has an ambiguous `autouse=` setting")
    return values[0].value


def _usefixtures_names(decorators: Iterable[ast.expr]) -> set[str]:
    """Return literal ``usefixtures`` names or fail closed on dynamic forms."""

    names: set[str] = set()
    for decorator in decorators:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "usefixtures"
        ):
            continue
        if (
            decorator.keywords
            or not decorator.args
            or any(
                not isinstance(argument, ast.Constant) or not isinstance(argument.value, str)
                for argument in decorator.args
            )
        ):
            raise AssertionError("pytest.mark.usefixtures must use literal fixture names")
        names.update(
            cast(str, argument.value)
            for argument in decorator.args
            if isinstance(argument, ast.Constant)
        )
    return names


def _module_usefixtures_names(tree: ast.Module) -> set[str]:
    """Return literal module-level ``usefixtures`` marks."""

    names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        values = (
            statement.value.elts
            if isinstance(statement.value, (ast.List, ast.Set, ast.Tuple))
            else [statement.value]
        )
        names.update(_usefixtures_names(values))
    return names


def _class_usefixtures_names(node: ast.ClassDef) -> set[str]:
    """Return literal class decorator and ``pytestmark`` fixture activation."""

    names = _usefixtures_names(node.decorator_list)
    for statement in node.body:
        if not isinstance(statement, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        values = (
            statement.value.elts
            if isinstance(statement.value, (ast.List, ast.Set, ast.Tuple))
            else [statement.value]
        )
        names.update(_usefixtures_names(values))
    return names


def _fixture_parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return statically named positional and keyword-only fixture parameters."""

    return {argument.arg for argument in [*node.args.args, *node.args.kwonlyargs]}


def _requested_fixture_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return literal request.getfixturevalue dependencies or fail closed."""

    names: set[str] = set()
    for call in ast.walk(node):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "getfixturevalue"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "request"
        ):
            continue
        if (
            len(call.args) != 1
            or call.keywords
            or not isinstance(call.args[0], ast.Constant)
            or not isinstance(call.args[0].value, str)
            or not call.args[0].value
        ):
            raise AssertionError("request.getfixturevalue requires one literal fixture name")
        names.add(call.args[0].value)
    return names


def _fixture_params_state(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    sources: dict[str, ast.expr],
    fixture_aliases: set[str],
    ambiguous_fixture_aliases: set[str],
    pytest_module_aliases: set[str],
    ambiguous_pytest_module_aliases: set[str],
) -> str:
    """Return ``none``, ``docs``, ``non-doc``, or ``ambiguous`` fixture params state."""

    decorators = [
        decorator
        for decorator in node.decorator_list
        if _is_pytest_fixture_decorator(
            decorator,
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        )
    ]
    if len(decorators) != 1 or not isinstance(decorators[0], ast.Call):
        return "none"
    params = [keyword.value for keyword in decorators[0].keywords if keyword.arg == "params"]
    if not params:
        return "none"
    if len(params) != 1:
        return "ambiguous"  # pragma: no cover - duplicate call keywords cannot be parsed
    values = _resolve_module_values(params[0], sources)
    if values is None or not values:
        return "ambiguous"
    if any(_expression_is_docs_markdown(value, set()) for value in values):
        return "docs"
    return "non-doc" if all(_string_constants(value) for value in values) else "ambiguous"


def _module_value_sources(tree: ast.Module) -> dict[str, ast.expr]:
    """Return uniquely assigned module names; rebinds intentionally stay unresolved."""

    sources: dict[str, ast.expr] = {}
    ambiguous: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in sources:
                ambiguous.add(target.id)
            else:
                sources[target.id] = statement.value
    for name in ambiguous:
        sources.pop(name, None)
    return sources


def _pytest_plugin_modules(tree: ast.Module, sources: dict[str, ast.expr]) -> set[str]:
    """Return literal repository plugin module names or fail closed on drift."""

    declarations = [
        statement.value
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and statement.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins"
            for target in (
                statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            )
        )
    ]
    if not declarations:
        return set()
    if len(declarations) != 1:
        raise AssertionError("pytest_plugins provenance is ambiguous after rebinding")
    values = _resolve_module_values(declarations[0], sources)
    if values is None or not values:
        raise AssertionError("pytest_plugins provenance requires literal module names")
    modules: set[str] = set()
    for value in values:
        if (
            not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
            or not value.value
        ):
            raise AssertionError("pytest_plugins provenance requires literal module names")
        modules.add(value.value)
    return modules


def _resolve_module_values(
    expression: ast.expr, sources: dict[str, ast.expr], seen: frozenset[str] = frozenset()
) -> list[ast.expr] | None:
    """Resolve one admitted static module value into leaf expressions, or ``None``."""

    if isinstance(expression, ast.Name):
        if expression.id in seen or expression.id not in sources:
            return None
        return _resolve_module_values(sources[expression.id], sources, seen | {expression.id})
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        if any(isinstance(item, ast.Starred) for item in expression.elts):
            return None
        values: list[ast.expr] = []
        for item in expression.elts:
            resolved = _resolve_module_values(item, sources, seen)
            if resolved is None:
                values.append(item)
            else:
                values.extend(resolved)
        return values
    return [expression]


def _parametrized_docs_and_ambiguous_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef, sources: dict[str, ast.expr]
) -> tuple[set[str], set[str]]:
    """Return docs and unresolved read-receiver parameters from static parametrization."""

    docs: set[str] = set()
    ambiguous: set[str] = set()
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize"
            and len(decorator.args) >= 2
        ):
            continue
        argnames = decorator.args[0]
        if isinstance(argnames, ast.Constant) and isinstance(argnames.value, str):
            names = [name.strip() for name in argnames.value.split(",")]
        elif isinstance(argnames, (ast.List, ast.Tuple)) and all(
            isinstance(value, ast.Constant) and isinstance(value.value, str)
            for value in argnames.elts
        ):
            names = [
                cast(str, value.value) for value in argnames.elts if isinstance(value, ast.Constant)
            ]
        else:
            ambiguous.update(_fixture_parameter_names(node))
            continue
        values = _resolve_module_values(decorator.args[1], sources)
        if values is None or len(names) != 1:
            ambiguous.update(names)
            continue
        for value in values:
            if _expression_is_docs_markdown(value, set()):
                docs.add(names[0])
            elif not _string_constants(value):
                ambiguous.add(names[0])
    positional = [*node.args.posonlyargs, *node.args.args]
    if node.args.defaults:
        for parameter, default in zip(
            positional[-len(node.args.defaults) :], node.args.defaults, strict=True
        ):
            if _expression_is_docs_markdown(default, set()):
                docs.add(parameter.arg)
            elif not _string_constants(default):
                ambiguous.add(parameter.arg)
    for parameter, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        if default is None:
            continue
        if _expression_is_docs_markdown(default, set()):
            docs.add(parameter.arg)
        elif not _string_constants(default):
            ambiguous.add(parameter.arg)
    return docs, ambiguous


def _indirect_fixture_parameter_states(
    node: ast.FunctionDef | ast.AsyncFunctionDef, sources: dict[str, ast.expr]
) -> tuple[set[str], set[str]]:
    """Return docs and ambiguous fixture names from admitted indirect parametrization."""

    docs: set[str] = set()
    ambiguous: set[str] = set()
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize"
            and len(decorator.args) >= 2
        ):
            continue
        indirect = [keyword.value for keyword in decorator.keywords if keyword.arg == "indirect"]
        if not indirect:
            continue
        if (
            len(indirect) != 1
            or not isinstance(decorator.args[0], ast.Constant)
            or not isinstance(decorator.args[0].value, str)
        ):
            ambiguous.update(_fixture_parameter_names(node))
            continue
        names = [name.strip() for name in decorator.args[0].value.split(",")]
        if len(names) != 1 or not names[0]:
            ambiguous.update(_fixture_parameter_names(node))
            continue
        indirect_value = indirect[0]
        admitted = (isinstance(indirect_value, ast.Constant) and indirect_value.value is True) or (
            isinstance(indirect_value, (ast.List, ast.Tuple, ast.Set))
            and len(indirect_value.elts) == 1
            and isinstance(indirect_value.elts[0], ast.Constant)
            and indirect_value.elts[0].value == names[0]
        )
        if not admitted:
            ambiguous.add(names[0])
            continue
        values = _resolve_module_values(decorator.args[1], sources)
        if values is None or not values or any(not _string_constants(value) for value in values):
            ambiguous.add(names[0])
        elif any(_expression_is_docs_markdown(value, set()) for value in values):
            docs.add(names[0])
    return docs, ambiguous


@dataclass
class _FunctionAnalysis:
    """One same-module function's direct docs-read status and call edges."""

    reads_directly: bool
    calls: set[str]
    unresolved: list[str]
    returns_docs: bool
    return_calls: set[str]
    return_names: set[str]
    call_result_assignments: dict[str, str]
    read_names: set[str]
    read_calls: set[str]
    parameter_names: tuple[str, ...]
    has_variadic_parameters: bool
    call_arguments: list[tuple[str, tuple[str, ...], dict[str, str]]]


def _repository_module_candidates(module: str, filename: str, repository_root: Path) -> set[Path]:
    """Return confined importer-relative/repository-root candidates for one module."""

    importer = Path(filename)
    if not importer.is_absolute():
        importer = repository_root / importer
    candidates: set[Path] = set()
    bases = [importer.parent, repository_root]
    if module.partition(".")[0] != "roastpilot_agent":
        bases.append(repository_root / "src")
    for base in bases:
        module_path = base.joinpath(*module.split("."))
        for path in (module_path.with_suffix(".py"), module_path / "__init__.py"):
            if path.is_file() and path.is_relative_to(repository_root):
                candidates.add(path)
    return candidates


def _repository_imported_fixture_analyses(
    tree: ast.Module,
    filename: str,
    repository_root: Path,
    plugin_modules: set[str] | None = None,
) -> tuple[dict[str, _FunctionAnalysis], set[str], set[str]]:
    """Return synthetic analyses for directly imported first-party fixtures.

    Fixtures imported into a test module are executable pytest dependencies even
    though their definition lives outside the module AST.  Each admitted import
    becomes a synthetic graph node; ambiguous local origins or fixture metadata
    are deliberately retained as named unresolved fixture names.
    """

    analyses: dict[str, _FunctionAnalysis] = {}
    ambiguous: set[str] = set()
    autouse: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or statement.module is None:
            continue
        candidates = _repository_module_candidates(statement.module, filename, repository_root)
        if not candidates:
            if statement.level:
                ambiguous.update(alias.asname or alias.name for alias in statement.names)
            continue
        if len(candidates) != 1:
            ambiguous.update(alias.asname or alias.name for alias in statement.names)
            continue
        source_path = next(iter(candidates))
        imported_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        (
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        ) = _pytest_fixture_aliases(imported_tree)
        functions = {
            node.name: node
            for node in imported_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for imported in statement.names:
            if imported.name not in functions:
                continue
            function = functions[imported.name]
            fixture_name = _fixture_exposed_name(
                function.name,
                function.decorator_list,
                fixture_aliases,
                ambiguous_fixture_aliases,
                pytest_module_aliases,
                ambiguous_pytest_module_aliases,
            )
            if fixture_name is None:
                continue
            source = source_path.read_text(encoding="utf-8")
            proxy = (
                source
                + "\n\ndef test_import_direct() -> None:\n"
                + f"    {function.name}()\n\n"
                + "def test_import_return() -> None:\n"
                + f"    {function.name}().read_text()\n"
            )
            readers = _docs_reading_test_modules(
                proxy, filename=str(source_path), repository_root=repository_root
            )
            analysis = _FunctionAnalysis(
                reads_directly="test_import_direct" in readers,
                calls=set(),
                unresolved=[],
                returns_docs="test_import_return" in readers,
                return_calls=set(),
                return_names=set(),
                call_result_assignments={},
                read_names=set(),
                read_calls=set(),
                parameter_names=(),
                has_variadic_parameters=False,
                call_arguments=[],
            )
            for name in {fixture_name, imported.asname or imported.name}:
                previous = analyses.get(name)
                if previous is not None and previous != analysis:
                    ambiguous.add(name)
                    analyses.pop(name, None)
                else:
                    analyses[name] = analysis
            if _fixture_is_autouse(
                function,
                fixture_aliases,
                ambiguous_fixture_aliases,
                pytest_module_aliases,
                ambiguous_pytest_module_aliases,
            ):
                autouse.update({fixture_name, imported.asname or imported.name})
    for module in plugin_modules or set():
        candidates = _repository_module_candidates(module, filename, repository_root)
        if len(candidates) != 1:
            ambiguous.add("__pytest_plugins__")
            continue
        source_path = next(iter(candidates))
        source = source_path.read_text(encoding="utf-8")
        imported_tree = ast.parse(source)
        (
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        ) = _pytest_fixture_aliases(imported_tree)
        functions = (
            node
            for node in imported_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            fixture_name = _fixture_exposed_name(
                function.name,
                function.decorator_list,
                fixture_aliases,
                ambiguous_fixture_aliases,
                pytest_module_aliases,
                ambiguous_pytest_module_aliases,
            )
            if fixture_name is None:
                continue
            proxy = (
                source
                + "\n\ndef test_import_direct() -> None:\n"
                + f"    {function.name}()\n\n"
                + "def test_import_return() -> None:\n"
                + f"    {function.name}().read_text()\n"
            )
            readers = _docs_reading_test_modules(
                proxy, filename=str(source_path), repository_root=repository_root
            )
            analysis = _FunctionAnalysis(
                reads_directly="test_import_direct" in readers,
                calls=set(),
                unresolved=[],
                returns_docs="test_import_return" in readers,
                return_calls=set(),
                return_names=set(),
                call_result_assignments={},
                read_names=set(),
                read_calls=set(),
                parameter_names=(),
                has_variadic_parameters=False,
                call_arguments=[],
            )
            previous = analyses.get(fixture_name)
            if previous is not None and previous != analysis:
                ambiguous.add(fixture_name)
                analyses.pop(fixture_name, None)
            else:
                analyses[fixture_name] = analysis
            if _fixture_is_autouse(
                function,
                fixture_aliases,
                ambiguous_fixture_aliases,
                pytest_module_aliases,
                ambiguous_pytest_module_aliases,
            ):
                autouse.add(fixture_name)
    return analyses, ambiguous, autouse


def _pytest_generate_tests_parameter_states(
    tree: ast.Module,
    functions: dict[str, _FunctionNode],
    sources: dict[str, ast.expr],
) -> dict[str, tuple[set[str], set[str]]]:
    """Return docs and ambiguous ``pytest_generate_tests`` parameter names per test.

    The admitted form is a literal ``metafunc.parametrize`` name and a
    statically folded value collection.  A dynamic value is fail-closed only
    for tests that actually accept that named receiver; unrelated hook code
    cannot create reader provenance for unrelated tests.
    """

    states = {
        name: (set[str](), set[str]())
        for name in functions
        if name.startswith("test_") or "::test_" in name
    }

    def condition_target(condition: ast.expr) -> set[str] | None:
        """Return static test names selected by one recognised hook guard."""

        pairs: list[tuple[ast.expr, ast.expr]] = []
        if (
            isinstance(condition, ast.Compare)
            and len(condition.ops) == len(condition.comparators) == 1
        ):
            pairs.append((condition.left, condition.comparators[0]))
            pairs.append((condition.comparators[0], condition.left))
        for left, right in pairs:
            if (
                isinstance(right, ast.Constant)
                and isinstance(right.value, str)
                and isinstance(left, ast.Attribute)
                and left.attr == "__name__"
                and isinstance(left.value, ast.Attribute)
                and left.value.attr == "function"
                and isinstance(left.value.value, ast.Name)
                and left.value.value.id == "metafunc"
            ):
                return {right.value}
            if (
                isinstance(right, ast.Name)
                and isinstance(left, ast.Attribute)
                and left.attr == "function"
                and isinstance(left.value, ast.Name)
                and left.value.id == "metafunc"
            ):
                return {right.id}
        return None

    def visit(nodes: list[ast.stmt], selected: set[str] | None) -> None:
        """Walk hook statements while retaining an optional static test selection."""

        for statement in nodes:
            if isinstance(statement, ast.If):
                target = condition_target(statement.test)
                visit(statement.body, target if target is not None else selected)
                visit(statement.orelse, selected)
                continue
            for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "parametrize"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "metafunc"
                    and len(call.args) >= 2
                ):
                    continue
                name_expression = call.args[0]
                if not isinstance(name_expression, ast.Constant) or not isinstance(
                    name_expression.value, str
                ):
                    continue
                names = [name.strip() for name in name_expression.value.split(",")]
                if len(names) != 1 or not names[0]:
                    continue
                values = _resolve_module_values(call.args[1], sources)
                docs = values is not None and any(
                    _expression_is_docs_markdown(value, set()) for value in values
                )
                ambiguous = values is None or any(
                    not _string_constants(value) for value in values or []
                )
                for test_name, node in functions.items():
                    if test_name not in states or (
                        selected is not None and test_name not in selected
                    ):
                        continue
                    parameters = _fixture_parameter_names(
                        cast(ast.FunctionDef | ast.AsyncFunctionDef, node)
                    )
                    if names[0] not in parameters:
                        continue
                    if docs:
                        states[test_name][0].add(names[0])
                    elif ambiguous:
                        states[test_name][1].add(names[0])

    for hook in tree.body:
        if (
            isinstance(hook, (ast.FunctionDef, ast.AsyncFunctionDef))
            and hook.name == "pytest_generate_tests"
        ):
            visit(hook.body, None)
    return states


def _class_attribute_states(
    tree: ast.Module, module_aliases: set[str], module_partial_aliases: set[str]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return statically docs-rooted and ambiguous class attribute names by class."""

    docs: dict[str, set[str]] = {}
    ambiguous: dict[str, set[str]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        values: dict[str, ast.expr] = {}
        duplicates: set[str] = set()
        for member in statement.body:
            if not isinstance(member, (ast.Assign, ast.AnnAssign)) or member.value is None:
                continue
            targets = member.targets if isinstance(member, ast.Assign) else [member.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    if target.id in values:
                        duplicates.add(target.id)
                    else:
                        values[target.id] = member.value
        for name, value in values.items():
            if name in duplicates:
                ambiguous.setdefault(statement.name, set()).add(name)
            elif _expression_is_docs_markdown(
                value, module_aliases
            ) or _expression_completes_partial_docs_alias(value, module_partial_aliases):
                docs.setdefault(statement.name, set()).add(name)
            elif _expression_has_docs_root(value) or not _string_constants(value):
                ambiguous.setdefault(statement.name, set()).add(name)
    return docs, ambiguous


def _function_parameter_names(func: _FunctionNode) -> tuple[str, ...]:
    """Return the statically bindable named parameters of one helper."""

    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ()
    arguments = func.args
    return tuple(
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    )


def _function_has_variadic_parameters(func: _FunctionNode) -> bool:
    """Return whether a helper admits unbounded positional or keyword binding."""

    return isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
        func.args.vararg is not None or func.args.kwarg is not None
    )


def _imported_parameter_read_names(func: _FunctionNode) -> set[str]:
    """Return direct reader parameters from one repository-local imported helper."""

    parameters = set(_function_parameter_names(func))
    return {
        call.func.value.id
        for call in ast.walk(func)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _READ_METHOD_NAMES
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in parameters
    }


def _repository_imported_function_analyses(
    tree: ast.Module, filename: str, repository_root: Path
) -> tuple[
    dict[str, _FunctionAnalysis],
    set[str],
    set[str],
    set[str],
    set[str],
    dict[tuple[str, str], str],
    set[tuple[str, str]],
    set[tuple[str, str]],
    set[tuple[str, str]],
]:
    """Return static analyses for direct imports from confined first-party modules."""

    analyses: dict[str, _FunctionAnalysis] = {}
    ambiguous: set[str] = set()
    imported_classes: set[str] = set()
    docs_values: set[str] = set()
    ambiguous_values: set[str] = set()
    module_calls: dict[tuple[str, str], str] = {}
    ambiguous_module_calls: set[tuple[str, str]] = set()
    module_docs_values: set[tuple[str, str]] = set()
    ambiguous_module_values: set[tuple[str, str]] = set()

    def add_module_members(module_alias: str, source_path: Path, members: set[str]) -> None:
        """Expose local module members through one explicitly imported alias."""

        imported_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in imported_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assignments = {
            target.id: node.value
            for node in imported_tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        for member, function in functions.items():
            if member not in members:
                continue
            key = (module_alias, member)
            qualified = f"{module_alias}.{member}"
            if key in module_calls:
                ambiguous_module_calls.add(key)
                continue
            source = source_path.read_text(encoding="utf-8")
            proxy = (
                source
                + "\n\ndef test_import_direct() -> None:\n"
                + f"    {function.name}()\n\n"
                + "def test_import_return() -> None:\n"
                + f"    {function.name}().read_text()\n"
            )
            readers = _docs_reading_test_modules(
                proxy, filename=str(source_path), repository_root=repository_root
            )
            analyses[qualified] = _FunctionAnalysis(
                reads_directly="test_import_direct" in readers,
                calls=set(),
                unresolved=[],
                returns_docs="test_import_return" in readers,
                return_calls=set(),
                return_names=set(),
                call_result_assignments={},
                read_names=_imported_parameter_read_names(function),
                read_calls=set(),
                parameter_names=_function_parameter_names(function),
                has_variadic_parameters=_function_has_variadic_parameters(function),
                call_arguments=[],
            )
            module_calls[key] = qualified
        for member, value in assignments.items():
            if member not in members:
                continue
            key = (module_alias, member)
            if _expression_is_docs_markdown(value, set()):
                module_docs_values.add(key)
            elif _expression_has_docs_root(value) or not _string_constants(value):
                ambiguous_module_values.add(key)

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                alias = imported.asname or imported.name.partition(".")[0]
                candidates = _repository_module_candidates(imported.name, filename, repository_root)
                if len(candidates) > 1:
                    ambiguous.add(alias)
                elif candidates:
                    members = {
                        attribute.attr
                        for attribute in ast.walk(tree)
                        if isinstance(attribute, ast.Attribute)
                        and isinstance(attribute.value, ast.Name)
                        and attribute.value.id == alias
                    }
                    add_module_members(alias, candidates.pop(), members)
            continue
        if not isinstance(statement, ast.ImportFrom) or statement.module is None:
            continue
        candidates = _repository_module_candidates(statement.module, filename, repository_root)
        if len(candidates) > 1:
            ambiguous.update(imported.asname or imported.name for imported in statement.names)
            continue
        if not candidates:
            if statement.level:
                ambiguous.update(imported.asname or imported.name for imported in statement.names)
            continue
        source_path = candidates.pop()
        imported_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in imported_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assignments = {
            target.id: node.value
            for node in imported_tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        classes = {node.name: node for node in imported_tree.body if isinstance(node, ast.ClassDef)}
        for imported in statement.names:
            alias = imported.asname or imported.name
            if imported.name == "*" or alias in analyses:
                ambiguous.add(alias)
                continue
            if imported.name not in functions:
                if imported.name in assignments:
                    value = assignments[imported.name]
                    if _expression_is_docs_markdown(value, set()):
                        docs_values.add(alias)
                    elif _expression_has_docs_root(value) or not _string_constants(value):
                        ambiguous_values.add(alias)
                elif imported.name not in classes:
                    ambiguous.add(alias)
                elif any(
                    isinstance(candidate, ast.Call)
                    and (
                        (
                            isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr in _READ_METHOD_NAMES
                        )
                        or (isinstance(candidate.func, ast.Name) and candidate.func.id == "open")
                    )
                    for candidate in ast.walk(classes[imported.name])
                ):
                    imported_classes.add(alias)
                continue
            source = source_path.read_text(encoding="utf-8")
            proxy = (
                source
                + "\n\ndef test_import_direct() -> None:\n"
                + f"    {imported.name}()\n\n"
                + "def test_import_return() -> None:\n"
                + f"    {imported.name}().read_text()\n"
            )
            readers = _docs_reading_test_modules(
                proxy, filename=str(source_path), repository_root=repository_root
            )
            analyses[alias] = _FunctionAnalysis(
                reads_directly="test_import_direct" in readers,
                calls=set(),
                unresolved=[],
                returns_docs="test_import_return" in readers,
                return_calls=set(),
                return_names=set(),
                call_result_assignments={},
                read_names=_imported_parameter_read_names(functions[imported.name]),
                read_calls=set(),
                parameter_names=_function_parameter_names(functions[imported.name]),
                has_variadic_parameters=_function_has_variadic_parameters(functions[imported.name]),
                call_arguments=[],
            )
    return (
        analyses,
        ambiguous,
        imported_classes,
        docs_values,
        ambiguous_values,
        module_calls,
        ambiguous_module_calls,
        module_docs_values,
        ambiguous_module_values,
    )


def _assert_no_imported_collected_test_callables(
    tree: ast.Module,
    filename: str,
    imported_analyses: dict[str, _FunctionAnalysis],
    ambiguous_imports: set[str],
) -> None:
    """Reject repository-local imported callables that pytest would collect as tests."""

    imported_test_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
        if (alias.asname or alias.name).startswith("test_")
        and (alias.asname or alias.name) in set(imported_analyses) | ambiguous_imports
    }
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                name = alias.asname or alias.name
                if name in imported_test_names:
                    raise AssertionError(
                        f"{filename}:{statement.lineno}: repository-local imported callable "
                        f"`{name}` would be collected as a test"
                    )
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in imported_test_names:
                raise AssertionError(
                    f"{filename}:{target.lineno}: imported collected test callable `{target.id}` "
                    "is reassigned"
                )


def _local_collected_callable_aliases(
    tree: ast.Module, functions: dict[str, _FunctionNode], filename: str
) -> dict[str, str]:
    """Resolve direct local callable aliases whose exported name pytest collects."""

    aliases: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if not isinstance(target, ast.Name) or not target.id.startswith("test_"):
                continue
            if target.id in aliases or not isinstance(statement.value, ast.Name):
                raise AssertionError(
                    f"{filename}:{target.lineno}: collected local callable alias `{target.id}` "
                    "is ambiguous"
                )
            if statement.value.id not in functions:
                raise AssertionError(
                    f"{filename}:{target.lineno}: collected local callable alias `{target.id}` "
                    "does not resolve to one local function"
                )
            aliases[target.id] = statement.value.id
    return aliases


_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _local_helpers(func: _FunctionNode) -> tuple[dict[str, _FunctionNode], set[str]]:
    """Collect statically named local def/async/lambda helpers in one function scope."""

    definitions: dict[str, list[_FunctionNode]] = {}
    assigned: set[str] = set()
    nodes: list[ast.AST] = list(func.body) if not isinstance(func, ast.Lambda) else [func.body]
    while nodes:
        node = nodes.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, []).append(node)
            nodes.extend(node.body)
            continue
        if isinstance(node, ast.ClassDef):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Lambda):
                    definitions.setdefault(target.id, []).append(node.value)
                else:
                    assigned.add(target.id)
        nodes.extend(ast.iter_child_nodes(node))
    helpers = {
        name: candidates[0]
        for name, candidates in definitions.items()
        if len(candidates) == 1 and name not in assigned
    }
    ambiguous = (set(definitions) - set(helpers)) | (assigned & set(definitions))
    return helpers, ambiguous


def _analyse_function(
    func: _FunctionNode,
    module_aliases: set[str],
    module_partial_aliases: set[str],
    module_value_sources: dict[str, ast.expr],
    known_functions: set[str],
    filename: str,
    class_name: str | None = None,
    class_methods: dict[str, str] | None = None,
    local_functions: dict[str, str] | None = None,
    ambiguous_local_functions: set[str] | None = None,
    module_calls: dict[tuple[str, str], str] | None = None,
    ambiguous_module_calls: set[tuple[str, str]] | None = None,
    module_docs_values: set[tuple[str, str]] | None = None,
    ambiguous_module_values: set[tuple[str, str]] | None = None,
    imported_classes: set[str] | None = None,
    builtin_open_import_aliases: set[str] | None = None,
    fixture_aliases: set[str] | None = None,
    ambiguous_fixture_aliases: set[str] | None = None,
    pytest_module_aliases: set[str] | None = None,
    ambiguous_pytest_module_aliases: set[str] | None = None,
    generated_docs_parameters: set[str] | None = None,
    generated_ambiguous_parameters: set[str] | None = None,
    class_docs_attributes: dict[str, set[str]] | None = None,
    class_ambiguous_attributes: dict[str, set[str]] | None = None,
    all_class_methods: dict[str, dict[str, str]] | None = None,
    relevant_helper_classes: set[str] | None = None,
    subprocess_module_aliases: set[str] | None = None,
    subprocess_call_aliases: set[str] | None = None,
    ambiguous_subprocess_aliases: set[str] | None = None,
) -> _FunctionAnalysis:
    """Walk one function body for a direct docs read, calls, and unresolved reads.

    A read receiver is "unresolved" when it has partial evidence the closed
    rule cannot prove either way: it folds a literal ``docs`` component but
    no provable ``.md``-suffixed segment, so it might be hiding a
    non-literal docs read the rule cannot otherwise trace.
    """

    aliases = set(module_aliases)
    partial_aliases = set(module_partial_aliases)
    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parameter_docs, ambiguous_parameters = _parametrized_docs_and_ambiguous_parameters(
            func, module_value_sources
        )
        aliases.update(parameter_docs)
        aliases.update(generated_docs_parameters or set())
        ambiguous_parameters.update(generated_ambiguous_parameters or set())
    else:
        ambiguous_parameters: set[str] = set()
    calls: set[str] = set()
    unresolved: list[str] = []
    has_docs_glob = False
    reads = False
    returns_docs = False
    fixture_params = (
        _fixture_params_state(
            func,
            module_value_sources,
            fixture_aliases or set(),
            ambiguous_fixture_aliases or set(),
            pytest_module_aliases or set(),
            ambiguous_pytest_module_aliases or set(),
        )
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        else "none"
    )
    request_param_aliases: set[str] = set()
    return_calls: set[str] = set()
    return_names: set[str] = set()
    call_result_assignments: dict[str, str] = {}
    read_names: set[str] = set()
    read_calls: set[str] = set()
    call_arguments: list[tuple[str, tuple[str, ...], dict[str, str]]] = []
    reader_aliases: set[str] = set()
    ambiguous_reader_aliases: set[str] = set()
    builtin_open_aliases: set[str] = set(builtin_open_import_aliases or set())
    ambiguous_builtin_open_aliases: set[str] = set()
    non_docs_aliases: set[str] = set()
    working_directory_state = "none"
    imported_class_instances: set[str] = set()
    ambiguous_imported_class_instances: set[str] = set()
    subprocess_command_docs_aliases: set[str] = set()
    subprocess_command_partial_aliases: set[str] = set()
    subprocess_command_ambiguous_aliases: set[str] = set()
    subprocess_command_non_docs_aliases: set[str] = set()
    subprocess_entry_aliases = set(subprocess_call_aliases or set())
    ambiguous_subprocess_entry_aliases = set(ambiguous_subprocess_aliases or set())
    helper_class_instances: dict[str, str] = {}
    ambiguous_helper_class_instances: set[str] = set()
    control_flow_docs_aliases = {
        target.id
        for conditional in ast.walk(func)
        if isinstance(conditional, ast.If)
        for assignment in ast.walk(conditional)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is not None
        for target in (
            assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        )
        if isinstance(target, ast.Name) and _expression_is_docs_markdown(assignment.value, set())
    }
    control_flow_subprocess_command_aliases = {
        target.id
        for conditional in ast.walk(func)
        if isinstance(conditional, ast.If)
        for assignment in ast.walk(conditional)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is not None
        for target in (
            assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        )
        if isinstance(target, ast.Name)
    }
    control_flow_reader_aliases = {
        target.id
        for conditional in ast.walk(func)
        if isinstance(conditional, ast.If)
        for assignment in ast.walk(conditional)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign))
        and isinstance(assignment.value, ast.Attribute)
        and assignment.value.attr in _READ_METHOD_NAMES
        and _expression_is_docs_markdown(assignment.value.value, set())
        for target in (
            assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        )
        if isinstance(target, ast.Name)
    }
    conditional_open_alias_states: dict[str, set[str]] = {}
    for conditional in ast.walk(func):
        if not isinstance(conditional, ast.If):
            continue
        for assignment in ast.walk(conditional):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            state = (
                "builtin"
                if (
                    isinstance(assignment.value, ast.Name)
                    and assignment.value.id in (builtin_open_import_aliases or set()) | {"open"}
                )
                or (
                    isinstance(assignment.value, ast.Attribute)
                    and assignment.value.attr == "open"
                    and isinstance(assignment.value.value, ast.Name)
                    and assignment.value.value.id == "builtins"
                )
                else "other"
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    conditional_open_alias_states.setdefault(target.id, set()).add(state)
    control_flow_imported_class_instances = {
        target.id
        for conditional in ast.walk(func)
        if isinstance(conditional, ast.If)
        for assignment in ast.walk(conditional)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign))
        and isinstance(assignment.value, ast.Call)
        and isinstance(assignment.value.func, ast.Name)
        and assignment.value.func.id in (imported_classes or set())
        for target in (
            assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        )
        if isinstance(target, ast.Name)
    }

    def _call_key(expression: ast.expr) -> str | None:
        """Resolve one admitted local, module, or containing-class call expression."""

        while isinstance(expression, ast.Await):
            expression = expression.value
        if not isinstance(expression, ast.Call):
            return None
        if isinstance(expression.func, ast.Name):
            return (local_functions or {}).get(expression.func.id) or (
                expression.func.id if expression.func.id in known_functions else None
            )
        if (
            isinstance(expression.func, ast.Attribute)
            and isinstance(expression.func.value, ast.Name)
            and (expression.func.value.id, expression.func.attr) in (module_calls or {})
        ):
            return (module_calls or {})[(expression.func.value.id, expression.func.attr)]
        if (
            isinstance(expression.func, ast.Attribute)
            and isinstance(expression.func.value, ast.Name)
            and expression.func.value.id in {"self", "cls", class_name}
        ):
            return (class_methods or {}).get(expression.func.attr)
        if isinstance(expression.func, ast.Attribute):
            receiver = expression.func.value
            helper_class: str | None = None
            if isinstance(receiver, ast.Name):
                helper_class = helper_class_instances.get(receiver.id)
                if receiver.id in (all_class_methods or {}):
                    helper_class = receiver.id
            elif (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id in (all_class_methods or {})
            ):
                helper_class = receiver.func.id
            if helper_class is not None:
                return (all_class_methods or {}).get(helper_class, {}).get(expression.func.attr)
        return None

    def _receiver_is_docs(target: ast.expr) -> bool:
        if isinstance(target, ast.Name):
            return target.id in aliases or has_docs_glob
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls", class_name}
            and target.attr in (class_docs_attributes or {}).get(class_name or "", set())
        ):
            return True
        return _expression_is_docs_markdown(target, aliases)

    def _receiver_is_partial(target: ast.expr) -> bool:
        if isinstance(target, ast.Name):
            return target.id in partial_aliases
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls", class_name}
            and target.attr in (class_ambiguous_attributes or {}).get(class_name or "", set())
        ):
            return True
        return _expression_has_docs_root(target)

    def _helper_class_instance(value: ast.expr) -> str | None:
        """Return one exact same-module helper-class instance provenance."""

        if isinstance(value, ast.Name):
            return helper_class_instances.get(value.id)
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in (all_class_methods or {})
        ):
            return value.func.id
        return None

    def _module_receiver_state(target: ast.expr) -> str:
        """Return local imported-module receiver provenance, if applicable."""

        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            return "none"
        key = (target.value.id, target.attr)
        if key in (module_docs_values or set()):
            return "docs"
        if key in (ambiguous_module_values or set()):
            return "ambiguous"
        return "none"

    def _argument_state(value: ast.expr) -> str:
        """Classify one helper argument under the existing closed path grammar."""

        if _receiver_is_docs(value) or _module_receiver_state(value) == "docs":
            return "docs"
        if _receiver_is_partial(value) or _module_receiver_state(value) == "ambiguous":
            return "ambiguous"
        return "non-docs"

    def _record_call(callee: str, call: ast.Call) -> None:
        """Record one resolved helper edge with bounded argument provenance."""

        calls.add(callee)
        positional = tuple(
            "ambiguous" if isinstance(argument, ast.Starred) else _argument_state(argument)
            for argument in call.args
        )
        keywords = {
            keyword.arg or "**": _argument_state(keyword.value) for keyword in call.keywords
        }
        call_arguments.append((callee, positional, keywords))

    def _reader_alias_state(value: ast.expr) -> str:
        """Return the fail-closed docs provenance of one bound reader method."""
        if not (isinstance(value, ast.Attribute) and value.attr in _READ_METHOD_NAMES):
            return "none"
        target = value.value
        if _receiver_is_docs(target) or _module_receiver_state(target) == "docs":
            return "docs"
        if isinstance(target, ast.Name) and target.id in non_docs_aliases:
            return "non-docs"
        if (
            _receiver_is_partial(target)
            or _module_receiver_state(target) == "ambiguous"
            or not _string_constants(target)
        ):
            return "ambiguous"
        if isinstance(target, ast.Name):
            return "ambiguous"
        return "non-docs"

    def _builtin_open_alias_state(value: ast.expr) -> str:
        """Return whether an assignment retains exact builtin-open provenance."""

        if isinstance(value, ast.Name) and value.id in builtin_open_aliases | {"open"}:
            return "builtin"
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "open"
            and isinstance(value.value, ast.Name)
            and value.value.id == "builtins"
        ):
            return "builtin"
        return "other"

    def _imported_class_instance_state(value: ast.expr) -> str:
        """Return exact or ambiguous imported-class-instance provenance for an assignment."""
        if isinstance(value, ast.Name) and value.id in imported_class_instances:
            return "known"
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in (imported_classes or set())
        ):
            return "known"
        if any(
            isinstance(candidate, ast.Name) and candidate.id in (imported_classes or set())
            for candidate in ast.walk(value)
        ):
            return "ambiguous"
        return "none"

    def _is_imported_class_instance(value: ast.expr) -> bool:
        """Return whether one call receiver is a known imported-class instance."""
        return (isinstance(value, ast.Name) and value.id in imported_class_instances) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in (imported_classes or set())
        )

    nodes: list[ast.AST] = list(func.body) if not isinstance(func, ast.Lambda) else [func.body]
    while nodes:
        node = nodes.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        nodes.extend(ast.iter_child_nodes(node))
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            full_match = _expression_is_docs_markdown(
                node.value, aliases
            ) or _expression_completes_partial_docs_alias(node.value, partial_aliases)
            partial_match = not full_match and (
                _expression_has_docs_root(node.value)
                or _expression_uses_alias(node.value, partial_aliases)
            )
            call_result = _call_key(node.value)
            reader_alias_state = _reader_alias_state(node.value)
            builtin_open_alias_state = _builtin_open_alias_state(node.value)
            imported_class_instance_state = _imported_class_instance_state(node.value)
            helper_class_instance = _helper_class_instance(node.value)
            for target in targets:
                if isinstance(target, ast.Name):
                    if (
                        isinstance(node.value, ast.Name)
                        and node.value.id in subprocess_entry_aliases
                    ) or (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr in _SUBPROCESS_ENTRY_POINTS
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id in (subprocess_module_aliases or set())
                    ):
                        subprocess_entry_aliases.add(target.id)
                        ambiguous_subprocess_entry_aliases.discard(target.id)
                    elif target.id in subprocess_entry_aliases:
                        subprocess_entry_aliases.discard(target.id)
                        ambiguous_subprocess_entry_aliases.add(target.id)
                    if target.id in control_flow_subprocess_command_aliases:
                        subprocess_command_docs_aliases.discard(target.id)
                        subprocess_command_partial_aliases.discard(target.id)
                        subprocess_command_ambiguous_aliases.add(target.id)
                        subprocess_command_non_docs_aliases.discard(target.id)
                    elif isinstance(node.value, (ast.List, ast.Tuple)):
                        static_container = all(
                            isinstance(element, ast.Constant) and isinstance(element.value, str)
                            for element in node.value.elts
                        )
                        if _expression_is_docs_markdown(node.value, aliases):
                            subprocess_command_docs_aliases.add(target.id)
                            subprocess_command_partial_aliases.discard(target.id)
                            subprocess_command_ambiguous_aliases.discard(target.id)
                            subprocess_command_non_docs_aliases.discard(target.id)
                        elif _expression_has_docs_root(node.value):
                            subprocess_command_docs_aliases.discard(target.id)
                            subprocess_command_partial_aliases.add(target.id)
                            subprocess_command_ambiguous_aliases.discard(target.id)
                            subprocess_command_non_docs_aliases.discard(target.id)
                        elif static_container:
                            subprocess_command_docs_aliases.discard(target.id)
                            subprocess_command_partial_aliases.discard(target.id)
                            subprocess_command_ambiguous_aliases.discard(target.id)
                            subprocess_command_non_docs_aliases.add(target.id)
                        else:
                            subprocess_command_docs_aliases.discard(target.id)
                            subprocess_command_partial_aliases.discard(target.id)
                            subprocess_command_ambiguous_aliases.add(target.id)
                            subprocess_command_non_docs_aliases.discard(target.id)
                    else:
                        subprocess_command_docs_aliases.discard(target.id)
                        subprocess_command_partial_aliases.discard(target.id)
                        subprocess_command_ambiguous_aliases.add(target.id)
                        subprocess_command_non_docs_aliases.discard(target.id)
                    if helper_class_instance is not None:
                        helper_class_instances[target.id] = helper_class_instance
                        ambiguous_helper_class_instances.discard(target.id)
                    elif (
                        isinstance(node.value, ast.Name)
                        and node.value.id in ambiguous_helper_class_instances
                    ) or (
                        isinstance(node.value, ast.Call)
                        and any(
                            isinstance(argument, ast.Name)
                            and argument.id in (all_class_methods or {})
                            for argument in node.value.args
                        )
                    ):
                        helper_class_instances.pop(target.id, None)
                        ambiguous_helper_class_instances.add(target.id)
                    else:
                        helper_class_instances.pop(target.id, None)
                        ambiguous_helper_class_instances.discard(target.id)
                    if imported_class_instance_state == "known":
                        imported_class_instances.add(target.id)
                        ambiguous_imported_class_instances.discard(target.id)
                    elif imported_class_instance_state == "ambiguous":
                        imported_class_instances.discard(target.id)
                        ambiguous_imported_class_instances.add(target.id)
                    elif target.id not in control_flow_imported_class_instances:
                        imported_class_instances.discard(target.id)
                        ambiguous_imported_class_instances.discard(target.id)
                    if builtin_open_alias_state == "builtin":
                        reader_aliases.discard(target.id)
                        ambiguous_reader_aliases.discard(target.id)
                    elif reader_alias_state == "docs":
                        reader_aliases.add(target.id)
                        ambiguous_reader_aliases.discard(target.id)
                    elif reader_alias_state == "ambiguous":
                        reader_aliases.discard(target.id)
                        ambiguous_reader_aliases.add(target.id)
                    elif reader_alias_state == "non-docs":
                        if target.id not in control_flow_reader_aliases:
                            reader_aliases.discard(target.id)
                            ambiguous_reader_aliases.discard(target.id)
                    elif target.id not in control_flow_reader_aliases:
                        reader_aliases.discard(target.id)
                        ambiguous_reader_aliases.discard(target.id)
                    if len(conditional_open_alias_states.get(target.id, set())) > 1:
                        builtin_open_aliases.discard(target.id)
                        ambiguous_builtin_open_aliases.add(target.id)
                    elif builtin_open_alias_state == "builtin":
                        builtin_open_aliases.add(target.id)
                        ambiguous_builtin_open_aliases.discard(target.id)
                    else:
                        builtin_open_aliases.discard(target.id)
                        ambiguous_builtin_open_aliases.discard(target.id)
                    if (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr == "param"
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "request"
                    ):
                        request_param_aliases.add(target.id)
                    if call_result is not None:
                        call_result_assignments[target.id] = call_result
                    if full_match:
                        aliases.add(target.id)
                        partial_aliases.discard(target.id)
                    elif partial_match:
                        partial_aliases.add(target.id)
                    elif target.id not in control_flow_docs_aliases:
                        aliases.discard(target.id)
                        partial_aliases.discard(target.id)
                    if full_match or partial_match:
                        non_docs_aliases.discard(target.id)
                    elif _string_constants(node.value):
                        non_docs_aliases.add(target.id)
                    else:
                        non_docs_aliases.discard(target.id)
        docs_root_aliases = aliases | partial_aliases
        if _is_docs_rooted_glob_call(node, docs_root_aliases) and not _is_docs_markdown_glob_call(
            node, docs_root_aliases
        ):
            unresolved.append(
                f"{filename}:{getattr(node, 'lineno', 0)}: docs-rooted glob must be exactly "
                "rglob('*.md')"
            )
        if isinstance(node, ast.For) and _is_docs_markdown_glob_call(node.iter, docs_root_aliases):
            has_docs_glob = True
            if isinstance(node.target, ast.Name):
                aliases.add(node.target.id)
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
        ):
            elements = node.iter.elts
            if elements and all(
                _expression_is_docs_markdown(element, aliases) for element in elements
            ):
                aliases.add(node.target.id)
                partial_aliases.discard(node.target.id)
                non_docs_aliases.discard(node.target.id)
            elif any(_expression_is_docs_markdown(element, aliases) for element in elements) or any(
                _expression_has_docs_root(element) or not _string_constants(element)
                for element in elements
            ):
                aliases.discard(node.target.id)
                partial_aliases.add(node.target.id)
                non_docs_aliases.discard(node.target.id)
            else:
                aliases.discard(node.target.id)
                partial_aliases.discard(node.target.id)
                non_docs_aliases.add(node.target.id)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in node.generators:
                if _is_docs_markdown_glob_call(generator.iter, docs_root_aliases) and isinstance(
                    generator.target, ast.Name
                ):
                    aliases.add(generator.target.id)
                if not (
                    isinstance(generator.target, ast.Name)
                    and isinstance(generator.iter, (ast.List, ast.Tuple, ast.Set))
                ):
                    continue
                elements = generator.iter.elts
                if elements and all(
                    _expression_is_docs_markdown(element, aliases) for element in elements
                ):
                    aliases.add(generator.target.id)
                    partial_aliases.discard(generator.target.id)
                    non_docs_aliases.discard(generator.target.id)
                elif any(
                    _expression_is_docs_markdown(element, aliases) for element in elements
                ) or any(
                    _expression_has_docs_root(element) or not _string_constants(element)
                    for element in elements
                ):
                    aliases.discard(generator.target.id)
                    partial_aliases.add(generator.target.id)
                    non_docs_aliases.discard(generator.target.id)
                else:
                    aliases.discard(generator.target.id)
                    partial_aliases.discard(generator.target.id)
                    non_docs_aliases.add(generator.target.id)
        if _is_docs_markdown_glob_call(node, docs_root_aliases):
            has_docs_glob = True
        if not isinstance(node, ast.Call):
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and node.value is not None:
                is_request_param = (
                    isinstance(node.value, ast.Attribute)
                    and node.value.attr == "param"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "request"
                ) or (isinstance(node.value, ast.Name) and node.value.id in request_param_aliases)
                if is_request_param and fixture_params == "docs":
                    returns_docs = True
                elif is_request_param and fixture_params in {"ambiguous", "none"}:
                    unresolved.append(
                        f"{filename}:{node.lineno}: fixture request.param provenance is ambiguous"
                    )
                elif _receiver_is_docs(node.value):
                    returns_docs = True
                elif isinstance(node.value, ast.Name):
                    return_names.add(node.value.id)
                elif (call_key := _call_key(node.value)) is not None:
                    return_calls.add(call_key)
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "addfinalizer"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "request"
        ):
            for callback in node.args:
                if not isinstance(callback, ast.Lambda):
                    unresolved.append(
                        f"{filename}:{node.lineno}: addfinalizer callback provenance is ambiguous"
                    )
                    continue
                for callback_call in (
                    nested for nested in ast.walk(callback.body) if isinstance(nested, ast.Call)
                ):
                    if not (
                        isinstance(callback_call.func, ast.Attribute)
                        and callback_call.func.attr in _READ_METHOD_NAMES
                    ):
                        continue
                    receiver = callback_call.func.value
                    if _expression_is_docs_markdown(receiver, aliases):
                        reads = True
                    elif _expression_has_docs_root(receiver) or not _string_constants(receiver):
                        unresolved.append(
                            f"{filename}:{callback_call.lineno}: addfinalizer callback receiver "
                            "is ambiguous"
                        )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "chdir"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "monkeypatch"
        ):
            if not node.args or isinstance(node.args[0], ast.Starred):
                working_directory_state = "ambiguous"
            elif isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                working_directory_state = (
                    "docs" if node.args[0].value.rstrip("/") in {"docs", "./docs"} else "non-docs"
                )
            else:
                working_directory_state = "ambiguous"
        subprocess_state = _subprocess_docs_call_state(
            node,
            subprocess_module_aliases or set(),
            subprocess_entry_aliases,
            ambiguous_subprocess_entry_aliases,
            subprocess_command_docs_aliases,
            subprocess_command_partial_aliases,
            subprocess_command_ambiguous_aliases,
            subprocess_command_non_docs_aliases,
        )
        if subprocess_state == "docs":
            reads = True
        elif subprocess_state == "ambiguous":
            unresolved.append(
                f"{filename}:{node.lineno}: subprocess docs-path provenance is ambiguous"
            )
        qualified_target = _qualified_open_target(node)
        if qualified_target is not None:
            target = qualified_target
            if (call_key := _call_key(target)) is not None:
                read_calls.add(call_key)
            if _receiver_is_docs(target) or _module_receiver_state(target) == "docs":
                reads = True
            elif _module_receiver_state(target) == "ambiguous":
                unresolved.append(
                    f"{filename}:{node.lineno}: imported module value provenance is ambiguous"
                )
            elif _receiver_is_partial(target):
                unresolved.append(
                    f"{filename}:{node.lineno}: qualified `open(...)` receiver folds a 'docs' "
                    "component with no provable '.md' segment — cannot prove this is "
                    "or is not a docs read"
                )
        elif isinstance(node.func, ast.Name):
            if node.func.id in reader_aliases:
                reads = True
            elif node.func.id in ambiguous_reader_aliases:
                unresolved.append(
                    f"{filename}:{node.lineno}: bound reader alias `{node.func.id}` is ambiguous"
                )
            elif node.func.id in ambiguous_builtin_open_aliases:
                unresolved.append(
                    f"{filename}:{node.lineno}: builtin open alias `{node.func.id}` is ambiguous"
                )
            elif node.func.id in builtin_open_aliases | {"open"}:
                try:
                    target = _builtin_open_target(node)
                except AssertionError as error:
                    unresolved.append(f"{filename}:{node.lineno}: {error}")
                    continue
                if (call_key := _call_key(target)) is not None:
                    read_calls.add(call_key)
                if _receiver_is_docs(target) or _module_receiver_state(target) == "docs":
                    reads = True
                elif _module_receiver_state(target) == "ambiguous":
                    unresolved.append(
                        f"{filename}:{node.lineno}: imported module value provenance is ambiguous"
                    )
                elif _receiver_is_partial(target):
                    unresolved.append(
                        f"{filename}:{node.lineno}: `open(...)` receiver folds a 'docs' "
                        "component with no provable '.md' segment — cannot prove this is "
                        "or is not a docs read"
                    )
            elif node.func.id in (local_functions or {}):
                _record_call((local_functions or {})[node.func.id], node)
            elif node.func.id in (ambiguous_local_functions or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved local helper edge `{node.func.id}()`"
                )
            elif node.func.id in known_functions:
                _record_call(node.func.id, node)
        elif isinstance(node.func, ast.Attribute) and _is_imported_class_instance(node.func.value):
            unresolved.append(
                f"{filename}:{node.lineno}: imported class instance method call provenance "
                "is ambiguous"
            )
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id in ambiguous_imported_class_instances)
        ):
            unresolved.append(
                f"{filename}:{node.lineno}: imported class instance provenance is ambiguous"
            )
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id in (all_class_methods or {})
        ):
            if (call_key := _call_key(node)) is not None:
                _record_call(call_key, node)
            elif node.func.value.func.id in (relevant_helper_classes or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved same-module helper-class call edge "
                    f"`{node.func.value.func.id}().{node.func.attr}()`"
                )
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHOD_NAMES:
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == "Path":
                if not node.args or isinstance(node.args[0], ast.Starred):
                    unresolved.append(
                        f"{filename}:{node.lineno}: unbound pathlib reader receiver is ambiguous"
                    )
                    continue
                target = node.args[0]
            if (call_key := _call_key(target)) is not None:
                read_calls.add(call_key)
            if isinstance(target, ast.Name):
                read_names.add(target.id)
                if target.id in ambiguous_parameters:
                    unresolved.append(
                        f"{filename}:{node.lineno}: parametrized receiver `{target.id}` "
                        "is ambiguous"
                    )
            relative_markdown_read = any(
                value.endswith(".md") and not value.startswith("/")
                for value in _string_constants(target)
            )
            if relative_markdown_read and working_directory_state == "docs":
                reads = True
            elif relative_markdown_read and working_directory_state == "ambiguous":
                unresolved.append(
                    f"{filename}:{node.lineno}: working-directory provenance is ambiguous"
                )
            elif _receiver_is_docs(target) or _module_receiver_state(target) == "docs":
                reads = True
            elif _module_receiver_state(target) == "ambiguous":
                unresolved.append(
                    f"{filename}:{node.lineno}: imported module value provenance is ambiguous"
                )
            elif _receiver_is_partial(target):
                unresolved.append(
                    f"{filename}:{node.lineno}: `.{node.func.attr}()` receiver folds a "
                    "'docs' component with no provable '.md' segment — cannot prove this "
                    "is or is not a docs read"
                )
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id in ambiguous_helper_class_instances:
                unresolved.append(
                    f"{filename}:{node.lineno}: same-module helper-class instance provenance "
                    "is ambiguous"
                )
                continue
            if (call_key := _call_key(node)) is not None:
                _record_call(call_key, node)
                continue
            receiver = node.func.value
            helper_class = helper_class_instances.get(receiver.id, receiver.id)
            if helper_class in (relevant_helper_classes or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved same-module helper-class call edge "
                    f"`{receiver.id}.{node.func.attr}()`"
                )
                continue
            if node.func.value.id in (imported_classes or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: imported class method call provenance is ambiguous"
                )
                continue
            module_key = (node.func.value.id, node.func.attr)
            if module_key in (module_calls or {}):
                _record_call((module_calls or {})[module_key], node)
                continue
            if module_key in (ambiguous_module_calls or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: imported module callable provenance is ambiguous"
                )
                continue
            receiver = node.func.value.id
            if receiver not in {"self", "cls", class_name}:
                continue
            callee = (class_methods or {}).get(node.func.attr)
            if callee is None:
                if class_name not in (relevant_helper_classes or set()) and not (
                    class_name or ""
                ).startswith("Test"):
                    continue
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved same-class call edge "
                    f"`{receiver}.{node.func.attr}()`"
                )
            else:
                _record_call(callee, node)
    return _FunctionAnalysis(
        reads_directly=reads,
        calls=calls,
        unresolved=unresolved,
        returns_docs=returns_docs,
        return_calls=return_calls,
        return_names=return_names,
        call_result_assignments=call_result_assignments,
        read_names=read_names,
        read_calls=read_calls,
        parameter_names=_function_parameter_names(func),
        has_variadic_parameters=_function_has_variadic_parameters(func),
        call_arguments=call_arguments,
    )


def _assert_no_unattributable_module_docs_reads(
    tree: ast.Module,
    module_aliases: set[str],
    functions: dict[str, _FunctionNode],
    filename: str,
) -> None:
    """Reject import-time docs reads that cannot be assigned to one collected test."""

    collected = {name for name in functions if name.startswith("test_")}
    collected.update(
        f"{statement.name}::{member.name}"
        for statement in tree.body
        if isinstance(statement, ast.ClassDef) and statement.name.startswith("Test")
        for member in statement.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and member.name.startswith("test_")
    )
    if not collected:
        return
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            receiver: ast.expr | None = None
            if isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHOD_NAMES:
                receiver = node.func.value
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                receiver = _builtin_open_target(node)
            if receiver is None:
                continue
            if _expression_is_docs_markdown(receiver, module_aliases):
                raise AssertionError(
                    f"{filename}:{node.lineno}: module-scope docs read cannot be attributed "
                    "to an exact collected test"
                )
            if _expression_has_docs_root(receiver):
                raise AssertionError(
                    f"{filename}:{node.lineno}: module-scope docs read receiver is ambiguous"
                )


def _assert_no_unaudited_collected_test_bases(tree: ast.Module, filename: str) -> None:
    """Reject collected test classes whose inherited methods are outside this module audit."""

    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or not statement.name.startswith("Test"):
            continue
        if statement.bases:
            bases = ", ".join(ast.unparse(base) for base in statement.bases)
            raise AssertionError(
                f"{filename}:{statement.lineno}: collected pytest class `{statement.name}` has "
                f"unaudited inherited base(s): {bases}"
            )


def _docs_reading_test_modules(
    source: str, filename: str = "<module>", repository_root: Path = _REPO
) -> set[str]:
    """Return the exact executable test names that read committed ``docs/**/*.md``.

    A test counts when it (or a same-module helper/fixture it calls or requests,
    transitively, bounded to :data:`_MAX_DOCS_CALL_GRAPH_DEPTH`) performs a
    provable docs-rooted ``read_text``/``read_bytes``/``open`` (method or
    builtin) call, or reads a ``Name`` bound by a docs-rooted
    ``glob("*.md")``/``rglob("*.md")`` loop or assignment. Comments and
    docstrings never create a call node and cannot satisfy or evade this
    rule.

    Args:
        source: The module's Python source.
        filename: A label used only in unresolved-call diagnostics.

    Returns:
        The set of qualified executable test prefixes that read docs content:
        top-level ``test_*`` names and ``ClassName::test_method`` names.

    Raises:
        AssertionError: If a read receiver has partial docs evidence (a
            folded ``docs`` component with no provable ``.md`` segment) that
            the closed rule cannot resolve either way. This fails the
            governance test with a named file:line reason instead of
            silently under- or over-marking.
    """

    tree = ast.parse(source)
    _assert_no_aliased_qualified_open_calls(tree, filename)
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "__test__"
            for target in statement.targets
        ):
            raise AssertionError(
                f"{filename}:{statement.lineno}: explicit __test__ callable provenance is ambiguous"
            )
    builtin_open_import_aliases = _builtin_open_import_aliases(tree)
    subprocess_module_aliases, subprocess_call_aliases, ambiguous_subprocess_aliases = (
        _subprocess_call_aliases(tree)
    )
    (
        imported_analyses,
        ambiguous_imports,
        imported_classes,
        imported_docs_values,
        ambiguous_imported_values,
        module_calls,
        ambiguous_module_calls,
        module_docs_values,
        ambiguous_module_values,
    ) = _repository_imported_function_analyses(tree, filename, repository_root)
    if "*" in ambiguous_imports:
        raise AssertionError(
            f"{filename}: wildcard repository-local import provenance is ambiguous"
        )
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("Test")
                and isinstance(statement.value, ast.Name)
                and statement.value.id in imported_classes
            ):
                raise AssertionError(
                    f"{filename}:{target.lineno}: imported test-class alias `{target.id}` "
                    "has unresolved reader provenance"
                )
    _assert_no_imported_collected_test_callables(
        tree, filename, imported_analyses, ambiguous_imports
    )
    module_value_sources = _module_value_sources(tree)
    plugin_modules = _pytest_plugin_modules(tree, module_value_sources)
    (
        imported_fixture_by_name,
        ambiguous_imported_fixtures,
        imported_autouse_fixtures,
    ) = _repository_imported_fixture_analyses(tree, filename, repository_root, plugin_modules)
    if "__pytest_plugins__" in ambiguous_imported_fixtures:
        raise AssertionError("pytest_plugins repository fixture provenance is ambiguous")
    imported_fixture_nodes = {
        fixture_name: f"<imported-fixture>::{fixture_name}"
        for fixture_name in imported_fixture_by_name
    }
    imported_fixture_analyses = {
        imported_fixture_nodes[fixture_name]: analysis
        for fixture_name, analysis in imported_fixture_by_name.items()
    }
    (
        fixture_aliases,
        ambiguous_fixture_aliases,
        pytest_module_aliases,
        ambiguous_pytest_module_aliases,
    ) = _pytest_fixture_aliases(tree)
    module_aliases: set[str] = set()
    module_partial_aliases: set[str] = set()
    module_aliases.update(imported_docs_values)
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if value is None:
                continue
            full_match = _expression_is_docs_markdown(
                value, module_aliases
            ) or _expression_completes_partial_docs_alias(value, module_partial_aliases)
            partial_match = not full_match and (
                _expression_has_docs_root(value)
                or _expression_uses_alias(value, module_partial_aliases)
            )
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if full_match:
                    module_aliases.add(target.id)
                    module_partial_aliases.discard(target.id)
                elif partial_match:
                    module_aliases.discard(target.id)
                    module_partial_aliases.add(target.id)
                else:
                    module_aliases.discard(target.id)
                    module_partial_aliases.discard(target.id)

    functions: dict[str, _FunctionNode] = {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    local_callable_aliases = _local_collected_callable_aliases(tree, functions, filename)
    functions.update({alias: functions[target] for alias, target in local_callable_aliases.items()})
    _assert_no_unaudited_collected_test_bases(tree, filename)
    _assert_no_unattributable_module_docs_reads(tree, module_aliases, functions, filename)
    class_methods: dict[str, dict[str, str]] = {}
    all_class_methods: dict[str, dict[str, str]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        members = [
            member
            for member in statement.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        methods: dict[str, str] = {}
        for member in members:
            qualified_name = f"{statement.name}::{member.name}"
            methods[member.name] = qualified_name
            functions[qualified_name] = member
        all_class_methods[statement.name] = methods
        if any(member.name.startswith("test_") for member in members):
            class_methods[statement.name] = methods
    class_docs_attributes, class_ambiguous_attributes = _class_attribute_states(
        tree, module_aliases, module_partial_aliases
    )
    relevant_helper_classes = {
        statement.name
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _READ_METHOD_NAMES
            and _expression_is_docs_markdown(call.func.value, module_aliases)
            for call in ast.walk(statement)
        )
    } | set(class_docs_attributes)
    generated_parameter_states = _pytest_generate_tests_parameter_states(
        tree, functions, module_value_sources
    )
    module_usefixtures = _module_usefixtures_names(tree)
    class_usefixtures = {
        statement.name: _class_usefixtures_names(statement)
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
    }
    module_fixture_nodes: dict[str, str] = {}
    class_fixture_nodes: dict[str, dict[str, str]] = {}
    for function_key, node in functions.items():
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fixture_name = _fixture_exposed_name(
            node.name,
            node.decorator_list,
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
        )
        if fixture_name is None:
            continue
        class_name = function_key.partition("::")[0] if "::" in function_key else None
        fixture_nodes = (
            class_fixture_nodes.setdefault(class_name, {})
            if class_name is not None
            else module_fixture_nodes
        )
        if fixture_name in fixture_nodes:
            raise AssertionError(
                f"fixtures `{fixture_nodes[fixture_name]}` and `{function_key}` expose "
                f"the duplicate name `{fixture_name}`"
            )
        fixture_nodes[fixture_name] = function_key
    for class_name, fixture_nodes in class_fixture_nodes.items():
        shared_names = module_fixture_nodes.keys() & fixture_nodes.keys()
        if shared_names:
            raise AssertionError(
                f"class `{class_name}` fixture name(s) collide with module fixtures: "
                f"{sorted(shared_names)}"
            )
    shared_imported_fixture_names = module_fixture_nodes.keys() & imported_fixture_nodes.keys()
    if shared_imported_fixture_names:
        raise AssertionError(
            f"module fixtures collide with imported fixture name(s): "
            f"{sorted(shared_imported_fixture_names)}"
        )
    fixtures = set(module_fixture_nodes.values()) | {
        fixture
        for fixture_nodes in class_fixture_nodes.values()
        for fixture in fixture_nodes.values()
    }

    def fixture_nodes_for(function_key: str) -> dict[str, str]:
        """Return the fail-closed fixture names available to one executable node."""

        class_name = function_key.partition("::")[0] if "::" in function_key else None
        if class_name is None:
            return module_fixture_nodes | imported_fixture_nodes
        return (
            module_fixture_nodes | imported_fixture_nodes | class_fixture_nodes.get(class_name, {})
        )

    def activated_fixture_nodes(
        function_key: str, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> set[str]:
        """Return literal autouse and usefixtures dependencies for one test."""

        available = fixture_nodes_for(function_key)
        class_name = function_key.partition("::")[0] if "::" in function_key else None
        requested = module_usefixtures | _usefixtures_names(node.decorator_list)
        requested |= _requested_fixture_names(node)
        if class_name is not None:
            requested |= class_usefixtures.get(class_name, set())
        ambiguous_requested = requested & ambiguous_imported_fixtures
        if ambiguous_requested:
            raise AssertionError(
                f"{filename}: imported fixture provenance is ambiguous for "
                f"{sorted(ambiguous_requested)}"
            )
        missing = requested - available.keys()
        if missing:
            raise AssertionError(
                f"{filename}: pytest fixture activation cannot resolve {sorted(missing)}"
            )
        autouse = {
            fixture_name
            for fixture_name in available.values()
            if fixture_name in functions
            if _fixture_is_autouse(
                cast(ast.FunctionDef | ast.AsyncFunctionDef, functions[fixture_name]),
                fixture_aliases,
                ambiguous_fixture_aliases,
                pytest_module_aliases,
                ambiguous_pytest_module_aliases,
            )
        }
        autouse.update(available[name] for name in imported_autouse_fixtures if name in available)
        return autouse | {available[name] for name in requested}

    known_functions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | set(imported_analyses)
    local_targets: dict[str, dict[str, str]] = {}
    ambiguous_local_targets: dict[str, set[str]] = {}
    local_keys: set[str] = set()
    for owner, node in list(functions.items()):
        helpers, ambiguous = (
            _local_helpers(node)
            if owner.startswith("test_") or "::test_" in owner
            else ({}, set[str]())
        )
        targets = {name: f"{owner}::<local>::{name}" for name in helpers}
        local_targets[owner] = targets
        ambiguous_local_targets[owner] = ambiguous
        for helper_name, helper in helpers.items():
            key = targets[helper_name]
            functions[key] = helper
            local_targets[key] = targets
            ambiguous_local_targets[key] = ambiguous
            local_keys.add(key)

    analyses: dict[str, _FunctionAnalysis] = {}
    unresolved: list[str] = []
    for name, node in functions.items():
        class_name = name.partition("::")[0] if "::" in name else None
        analysis = _analyse_function(
            node,
            module_aliases,
            module_partial_aliases,
            module_value_sources,
            known_functions,
            filename,
            class_name,
            all_class_methods.get(class_name) if class_name else None,
            local_targets.get(name),
            ambiguous_local_targets.get(name),
            module_calls,
            ambiguous_module_calls,
            module_docs_values,
            ambiguous_module_values,
            imported_classes,
            builtin_open_import_aliases,
            fixture_aliases,
            ambiguous_fixture_aliases,
            pytest_module_aliases,
            ambiguous_pytest_module_aliases,
            generated_parameter_states.get(name, (set(), set()))[0],
            generated_parameter_states.get(name, (set(), set()))[1],
            class_docs_attributes,
            class_ambiguous_attributes,
            all_class_methods,
            relevant_helper_classes,
            subprocess_module_aliases,
            subprocess_call_aliases,
            ambiguous_subprocess_aliases,
        )
        analyses[name] = analysis
        if name not in local_keys:
            unresolved.extend(analysis.unresolved)
    if unresolved:
        raise AssertionError(
            "docs-content governance found an unresolved dynamic read receiver that "
            f"could hide an unmarked docs read: {unresolved}"
        )
    analyses.update(imported_analyses)
    analyses.update(imported_fixture_analyses)

    pytest_decorator_modules = pytest_module_aliases | {"pytest"}
    local_decorator_functions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    decorator_aliases: dict[str, str] = {}
    ambiguous_decorator_names: set[str] = set()
    pytest_marker_aliases: set[str] = set()
    pytest_marker_module_aliases: set[str] = set()

    def pytest_marker_alias_kind(value: ast.expr) -> str | None:
        """Return the exact pytest marker alias form for one bare assignment."""

        value = value.func if isinstance(value, ast.Call) else value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "mark"
            and isinstance(value.value, ast.Name)
            and value.value.id in pytest_decorator_modules
        ):
            return "module"
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Attribute)
            and value.value.attr == "mark"
            and isinstance(value.value.value, ast.Name)
            and value.value.value.id in pytest_decorator_modules
        ):
            return "decorator"
        return None

    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            for target in (
                name
                for assigned in targets
                for name in ast.walk(assigned)
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
            ):
                decorator_aliases.pop(target.id, None)
                pytest_marker_aliases.discard(target.id)
                pytest_marker_module_aliases.discard(target.id)
                ambiguous_decorator_names.add(target.id)
            continue
        target = targets[0].id
        if target in (
            local_decorator_functions
            | decorator_aliases.keys()
            | ambiguous_decorator_names
            | pytest_marker_aliases
            | pytest_marker_module_aliases
        ):
            decorator_aliases.pop(target, None)
            pytest_marker_aliases.discard(target)
            pytest_marker_module_aliases.discard(target)
            ambiguous_decorator_names.add(target)
            continue
        if isinstance(statement.value, ast.Name):
            if statement.value.id in ambiguous_decorator_names:
                ambiguous_decorator_names.add(target)
                continue
            if statement.value.id in local_decorator_functions:
                decorator_aliases[target] = statement.value.id
                continue
            if statement.value.id in decorator_aliases:
                decorator_aliases[target] = decorator_aliases[statement.value.id]
                continue
            if statement.value.id in pytest_marker_aliases:
                pytest_marker_aliases.add(target)
                continue
            if statement.value.id in pytest_marker_module_aliases:
                pytest_marker_module_aliases.add(target)
                continue
        marker_alias_kind = pytest_marker_alias_kind(statement.value)
        if marker_alias_kind == "module":
            pytest_marker_module_aliases.add(target)
        elif marker_alias_kind == "decorator":
            pytest_marker_aliases.add(target)
        else:
            ambiguous_decorator_names.add(target)
    for name, node in functions.items():
        if name in local_keys or (not name.startswith("test_") and "::test_" not in name):
            continue
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name):
                if target.id in ambiguous_decorator_names:
                    raise AssertionError(
                        f"{filename}:{decorator.lineno}: decorator provenance for "
                        f"`{target.id}` is ambiguous"
                    )
                if target.id in pytest_marker_aliases:
                    continue
                if target.id in decorator_aliases:
                    analyses[name].calls.add(decorator_aliases[target.id])
                    continue
                if target.id in analyses:
                    analyses[name].calls.add(target.id)
                continue
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
                continue
            if target.value.id in pytest_decorator_modules | pytest_marker_module_aliases:
                continue
            key = (target.value.id, target.attr)
            if key in module_calls:
                analyses[name].calls.add(module_calls[key])
            elif key in ambiguous_module_calls:
                raise AssertionError(
                    f"{filename}:{decorator.lineno}: decorator provenance for "
                    f"`{target.value.id}.{target.attr}` is ambiguous"
                )

    class_decorators = {
        statement.name: statement.decorator_list
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
    }

    def decorator_reader_state(decorators: Iterable[ast.expr]) -> str:
        """Return closed import-time reader provenance for collected decorators."""

        state = "none"
        for decorator in decorators:
            for call in (node for node in ast.walk(decorator) if isinstance(node, ast.Call)):
                if not (
                    isinstance(call.func, ast.Attribute) and call.func.attr in _READ_METHOD_NAMES
                ):
                    continue
                receiver = call.func.value
                if _expression_is_docs_markdown(receiver, module_aliases):
                    state = "docs"
                elif _expression_has_docs_root(receiver) or not _string_constants(receiver):
                    return "ambiguous"
        return state

    for name, node in functions.items():
        if name in local_keys or (not name.startswith("test_") and "::test_" not in name):
            continue
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        class_name = name.partition("::")[0] if "::" in name else None
        state = decorator_reader_state(
            [*node.decorator_list, *(class_decorators.get(class_name, []) if class_name else [])]
        )
        if state == "docs":
            analyses[name].reads_directly = True
        elif state == "ambiguous":
            raise AssertionError(
                f"{filename}:{node.lineno}: decorator read provenance is ambiguous"
            )

    module_xunit_hooks = {
        name
        for name in ("setup_module", "teardown_module", "setup_function", "teardown_function")
        if name in analyses
    }
    for name in functions:
        if name.startswith("test_"):
            analyses[name].calls.update(module_xunit_hooks)
        if "::test_" not in name:
            continue
        class_name = name.partition("::")[0]
        analyses[name].calls.update(
            f"{class_name}::{hook}"
            for hook in ("setup_class", "teardown_class", "setup_method", "teardown_method")
            if f"{class_name}::{hook}" in analyses
        )

    def bound_argument_state(
        callee: _FunctionAnalysis,
        parameter: str,
        positional: tuple[str, ...],
        keywords: dict[str, str],
    ) -> str:
        """Return a closed binding state for one reader parameter at one call site."""

        if "**" in keywords or any(state == "ambiguous" for state in positional):
            return "ambiguous"
        try:
            position = callee.parameter_names.index(parameter)
        except ValueError:
            return "ambiguous"
        positional_state = positional[position] if position < len(positional) else None
        keyword_state = keywords.get(parameter)
        unknown_keywords = set(keywords) - set(callee.parameter_names)
        if unknown_keywords and not callee.has_variadic_parameters:
            return "ambiguous"
        if positional_state is not None and keyword_state is not None:
            return "ambiguous"
        if positional_state is not None:
            return positional_state
        if keyword_state is not None:
            return keyword_state
        return "non-docs"

    for analysis in analyses.values():
        for callee_name, positional, keywords in analysis.call_arguments:
            callee = analyses.get(callee_name)
            if callee is None:
                continue
            for parameter in callee.read_names & set(callee.parameter_names):
                state = bound_argument_state(callee, parameter, positional, keywords)
                if state == "docs":
                    analysis.reads_directly = True
                elif state == "ambiguous":
                    analysis.unresolved.append(
                        f"{filename}: helper argument binding for `{callee_name}` is ambiguous"
                    )
    for name, node in functions.items():
        if (
            name in local_keys
            or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or (not name.startswith("test_") and "::test_" not in name)
        ):
            continue
        if any(
            (isinstance(call.func, ast.Name) and call.func.id in ambiguous_imports)
            or (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in ambiguous_imports
            )
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        ):
            raise AssertionError(
                f"{filename}: imported callable provenance for `{name}` is ambiguous"
            )
        if analyses[name].read_names & ambiguous_imported_values:
            raise AssertionError(f"{filename}: imported value provenance for `{name}` is ambiguous")

    for fixture_name in fixtures:
        fixture = functions[fixture_name]
        assert isinstance(fixture, (ast.FunctionDef, ast.AsyncFunctionDef))
        available_fixture_nodes = fixture_nodes_for(fixture_name)
        ambiguous_parameters = _fixture_parameter_names(fixture) & ambiguous_imported_fixtures
        if ambiguous_parameters:
            raise AssertionError(
                f"{filename}: imported fixture provenance is ambiguous for "
                f"{sorted(ambiguous_parameters)}"
            )
        fixture_parameters = {
            available_fixture_nodes[parameter]
            for parameter in _fixture_parameter_names(fixture)
            if parameter in available_fixture_nodes
        }
        requested_fixture_names = _requested_fixture_names(fixture)
        missing_requested = requested_fixture_names - available_fixture_nodes.keys()
        if missing_requested:
            raise AssertionError(
                f"{filename}: request.getfixturevalue cannot resolve {sorted(missing_requested)}"
            )
        fixture_parameters |= {available_fixture_nodes[name] for name in requested_fixture_names}
        analyses[fixture_name].calls.update(fixture_parameters)
        analyses[fixture_name].return_calls.update(
            available_fixture_nodes[parameter]
            for parameter in analyses[fixture_name].return_names
            if parameter in available_fixture_nodes
        )

    changed = True
    while changed:
        changed = False
        for analysis in analyses.values():
            if not analysis.returns_docs and any(
                analyses[callee].returns_docs
                for callee in analysis.return_calls
                if callee in analyses
            ):
                analysis.returns_docs = True
                changed = True
    for analysis in analyses.values():
        if any(
            target in analysis.read_names and callee in analyses and analyses[callee].returns_docs
            for target, callee in analysis.call_result_assignments.items()
        ):
            analysis.reads_directly = True
        if any(
            callee in analyses and analyses[callee].returns_docs for callee in analysis.read_calls
        ):
            analysis.reads_directly = True

    def reads_transitively(name: str, seen: frozenset[str]) -> bool:
        if name in seen or name not in analyses:
            return False
        if len(seen) >= _MAX_DOCS_CALL_GRAPH_DEPTH:
            raise AssertionError(
                "docs-content governance exceeded the bounded same-module call graph at "
                f"`{name}`; cannot prove whether it reads docs content"
            )
        analysis = analyses[name]
        if analysis.unresolved:
            raise AssertionError(
                "docs-content governance found an unresolved local helper edge that "
                f"could hide an unmarked docs read: {analysis.unresolved}"
            )
        if analysis.reads_directly:
            return True
        next_seen = seen | {name}
        return any(reads_transitively(callee, next_seen) for callee in analysis.calls)

    readers: set[str] = set()
    for name, node in functions.items():
        if name in local_keys:
            continue
        if not name.startswith("test_") and "::test_" not in name:
            continue
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        available_fixture_nodes = fixture_nodes_for(name)
        ambiguous_parameters = _fixture_parameter_names(node) & ambiguous_imported_fixtures
        if ambiguous_parameters:
            raise AssertionError(
                f"{filename}: imported fixture provenance is ambiguous for "
                f"{sorted(ambiguous_parameters)}"
            )
        fixture_edges = {
            available_fixture_nodes[parameter]
            for parameter in _fixture_parameter_names(node)
            if parameter in available_fixture_nodes
        }
        fixture_edges |= activated_fixture_nodes(name, node)
        indirect_docs, indirect_ambiguous = _indirect_fixture_parameter_states(
            node, module_value_sources
        )
        reads_indirect_fixture_param = False
        for parameter, fixture_name in available_fixture_nodes.items():
            if fixture_name not in functions or not isinstance(
                functions[fixture_name], (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            consumes_request_param = any(
                isinstance(candidate, ast.Attribute)
                and candidate.attr == "param"
                and isinstance(candidate.value, ast.Name)
                and candidate.value.id == "request"
                for candidate in ast.walk(functions[fixture_name])
            )
            if not consumes_request_param:
                continue
            if parameter in indirect_ambiguous:
                raise AssertionError(
                    f"{filename}: indirect fixture parameter provenance is ambiguous for "
                    f"`{parameter}`"
                )
            if parameter in indirect_docs:
                reads_indirect_fixture_param = True
        reads_fixture_value = any(
            parameter in analyses[name].read_names
            and fixture_name in analyses
            and analyses[fixture_name].returns_docs
            for parameter, fixture_name in available_fixture_nodes.items()
        )
        if (
            reads_indirect_fixture_param
            or reads_fixture_value
            or reads_transitively(name, frozenset())
            or any(reads_transitively(fixture_name, frozenset()) for fixture_name in fixture_edges)
        ):
            readers.add(name)
    return readers


def _function_has_docs_marker(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether a function carries the exact ``@pytest.mark.docs`` decorator."""

    return any(ast.unparse(decorator) == "pytest.mark.docs" for decorator in node.decorator_list)


def _marked_docs_test_prefixes(tree: ast.Module) -> set[str]:
    """Return exact test prefixes carrying a method- or class-level docs marker."""

    marked: set[str] = set()
    local_markers = {
        statement.name: _function_has_docs_marker(statement)
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("test_") and _function_has_docs_marker(statement):
                marked.add(statement.name)
            continue
        if not isinstance(statement, ast.ClassDef):
            continue
        class_marked = any(
            ast.unparse(decorator) == "pytest.mark.docs" for decorator in statement.decorator_list
        )
        for member in statement.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if member.name.startswith("test_") and (
                class_marked or _function_has_docs_marker(member)
            ):
                marked.add(f"{statement.name}::{member.name}")
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not isinstance(statement.value, ast.Name) or not local_markers.get(statement.value.id):
            continue
        marked.update(
            target.id
            for target in targets
            if isinstance(target, ast.Name) and target.id.startswith("test_")
        )
    return marked


def _mark_value_has_marker(value: ast.expr, marker: str) -> bool:
    """Return whether a literal pytest mark expression carries one marker."""

    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        return any(_mark_value_has_marker(item, marker) for item in value.elts)
    target = value.func if isinstance(value, ast.Call) else value
    return (
        isinstance(target, ast.Attribute)
        and target.attr == marker
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
        and isinstance(target.value.value, ast.Name)
        and target.value.value.id == "pytest"
    )


def _pytestmark_has_marker(statements: Iterable[ast.stmt], marker: str) -> bool:
    """Return whether a module/class body gives tests an effective marker."""

    return any(
        isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        )
        and _mark_value_has_marker(statement.value, marker)
        for statement in statements
    )


def _effective_marked_test_prefixes(tree: ast.Module, marker: str) -> set[str]:
    """Return test prefixes inheriting ``marker`` from all supported scopes."""

    module_marked = _pytestmark_has_marker(tree.body, marker)
    marked: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("test_") and (
                module_marked
                or any(_mark_value_has_marker(item, marker) for item in statement.decorator_list)
            ):
                marked.add(statement.name)
        elif isinstance(statement, ast.ClassDef):
            class_marked = (
                module_marked
                or _pytestmark_has_marker(statement.body, marker)
                or any(_mark_value_has_marker(item, marker) for item in statement.decorator_list)
            )
            for member in statement.body:
                if (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name.startswith("test_")
                    and (
                        class_marked
                        or any(
                            _mark_value_has_marker(item, marker) for item in member.decorator_list
                        )
                    )
                ):
                    marked.add(f"{statement.name}::{member.name}")
    return marked


def _assert_exact_docs_markers(tree: ast.Module, readers: set[str], filename: str) -> None:
    """Fail when exact docs-reader prefixes and effective docs markers differ."""

    marked = _marked_docs_test_prefixes(tree)
    assert marked == readers, (
        f"{filename}: @pytest.mark.docs must sit on exactly the readers "
        f"{sorted(readers)}, found {sorted(marked)}"
    )
    assert not readers & _effective_marked_test_prefixes(tree, "stress"), (
        f"{filename}: docs readers cannot carry pytest.mark.stress because the docs selector "
        "excludes stress"
    )


def _is_proven_docs_free_pytestmark(value: ast.expr) -> bool:
    """Return whether a ``pytestmark`` value statically excludes ``docs``.

    Only literal list, tuple, and set containers of direct pytest marker
    expressions are admitted. A dynamic or otherwise unrecognised expression
    may resolve to ``pytest.mark.docs`` at collection time and is unsafe.
    """

    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        return all(_is_proven_docs_free_pytestmark(element) for element in value.elts)
    marker = value.func if isinstance(value, ast.Call) else value
    return (
        isinstance(marker, ast.Attribute)
        and marker.attr != "docs"
        and isinstance(marker.value, ast.Attribute)
        and marker.value.attr == "mark"
        and isinstance(marker.value.value, ast.Name)
        and marker.value.value.id == "pytest"
    )


def _module_has_unsafe_pytestmark(tree: ast.Module) -> bool:
    """Return whether a module ``pytestmark`` can violate exact docs selection.

    Module-level docs marks and marks that cannot statically prove ``docs`` is
    absent are both retired because either can broad-mark non-reader tests.
    """

    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        if not _is_proven_docs_free_pytestmark(statement.value):
            return True
    return False


def _test_module_paths(tests_root: Path) -> list[Path]:
    """Return every configured-default pytest test module below ``tests_root``."""

    return sorted(
        {path for pattern in ("test_*.py", "*_test.py") for path in tests_root.rglob(pattern)}
    )


def _conftest_paths(tests_root: Path) -> list[Path]:
    """Return every pytest conftest below the configured test root in stable order."""

    return sorted(tests_root.rglob("conftest.py"))


def _assert_conftests_docs_free(tests_root: Path) -> None:
    """Reject conftest provenance that can hide docs reads from test markers."""

    for path in _conftest_paths(tests_root):
        relative_path = (
            path.relative_to(_REPO).as_posix() if path.is_relative_to(_REPO) else str(path)
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        probe = (
            source
            + "\n\ndef test_conftest_docs_provenance_probe() -> None:\n"
            + "".join(f"    {name}().read_text()\n" for name in functions)
        )
        if _docs_reading_test_modules(probe, filename=relative_path):
            raise AssertionError(f"{relative_path}: conftest exposes docs Markdown provenance")


def _assert_default_pytest_collection_options(options: dict[str, object]) -> None:
    """Fail if pytest collection no longer uses the audited default test shapes."""

    assert "python_files" not in options, "pytest python_files must remain unset"
    assert options.get("testpaths") == ["tests"], "pytest testpaths must remain exactly ['tests']"
    assert options.get("python_functions") == ["test_*"], (
        "pytest python_functions must remain exactly ['test_*']"
    )


@pytest.mark.docs_ci
def test_docs_reading_tests_carry_the_exact_docs_marker_and_nothing_else() -> None:
    """Every committed-Markdown reader is marked at the exact function, module-wide."""

    markdown_readers: dict[Path, set[str]] = {}
    _assert_conftests_docs_free(_REPO / "tests")
    for path in _test_module_paths(_REPO / "tests"):
        relative_path = path.relative_to(_REPO).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not _module_has_unsafe_pytestmark(tree), (
            f"{relative_path}: module-level pytestmark is docs-bearing or ambiguous; "
            "only statically proven docs-free literal marker forms are allowed"
        )
        readers = _docs_reading_test_modules(source, filename=relative_path)
        if readers:
            markdown_readers[path] = readers
        _assert_exact_docs_markers(tree, readers, relative_path)

    assert set(markdown_readers) == {
        _REPO / "tests" / "test_agent_model_pins.py",
        _REPO / "tests" / "test_agent_worktree_controls.py",
        _REPO / "tests" / "test_capture_agent_usage.py",
        _REPO / "tests" / "test_config.py",
        _REPO / "tests" / "test_worktree_gate_recipe.py",
    }
    assert (
        _docs_reading_test_modules(
            "def test_comment() -> None:\n"
            '    """docs/only-a-docstring.md"""\n'
            "    # docs/comment.md\n"
            "    assert True\n"
        )
        == set()
    )


@pytest.mark.docs_ci
def test_docs_governance_discovers_nested_test_modules(tmp_path: Path) -> None:
    """Both configured default nested pytest filename forms remain visible."""

    tests_root = tmp_path / "tests"
    nested = tests_root / "nested"
    nested.mkdir(parents=True)
    (tests_root / "test_top_level.py").write_text("def test_top() -> None: pass\n")
    (nested / "test_nested.py").write_text("def test_nested() -> None: pass\n")
    (nested / "suffix_test.py").write_text("def test_suffix() -> None: pass\n")
    (nested / "helper.py").write_text("pass\n")
    assert [path.relative_to(tests_root).as_posix() for path in _test_module_paths(tests_root)] == [
        "nested/suffix_test.py",
        "nested/test_nested.py",
        "test_top_level.py",
    ]


@pytest.mark.docs_ci
def test_docs_governance_rejects_imported_collected_test_callables(tmp_path: Path) -> None:
    """Repository-local imports cannot create unanalysed pytest test callables."""

    (tmp_path / "helpers.py").write_text(
        "def test_docs() -> None:\n    pass\n\ndef docs_helper() -> None:\n    pass\n",
        encoding="utf-8",
    )
    for source in (
        "from helpers import test_docs\n",
        "from helpers import docs_helper as test_docs\n",
        "from helpers import docs_helper as test_docs\ntest_docs = lambda: None\n",
    ):
        with pytest.raises(AssertionError, match="imported callable `test_docs`|reassigned"):
            _docs_reading_test_modules(source, filename="test_import.py", repository_root=tmp_path)
    assert (
        _docs_reading_test_modules(
            "from helpers import docs_helper as helper\n",
            filename="test_import.py",
            repository_root=tmp_path,
        )
        == set()
    )


@pytest.mark.docs_ci
def test_docs_governance_follows_repository_local_imported_helpers(tmp_path: Path) -> None:
    """Repository-local imported helper reads and return paths reach consumers."""

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "_agent_defs.py").write_text(
        "from pathlib import Path\n\n"
        "def docs_reader() -> str:\n    return Path('docs/imported.md').read_text()\n\n"
        "def docs_path() -> Path:\n    return Path('docs/imported-path.md')\n\n"
        "def non_docs() -> str:\n    return Path('config/imported.md').read_text()\n"
    )
    source = (
        "from _agent_defs import docs_reader, docs_path, non_docs\n\n"
        "def test_direct() -> None:\n    assert docs_reader()\n\n"
        "def test_returned() -> None:\n    docs_path().read_text()\n\n"
        "def test_non_docs() -> None:\n    assert non_docs()\n"
    )
    assert _docs_reading_test_modules(
        source, filename=str(tests_root / "test_imported.py"), repository_root=tmp_path
    ) == {"test_direct", "test_returned"}
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(
            "from _agent_defs import missing\n\ndef test_ambiguous() -> None:\n    missing()\n",
            filename=str(tests_root / "test_imported.py"),
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_propagates_helper_reader_parameters(tmp_path: Path) -> None:
    """Resolved local and imported helper parameter bindings retain docs provenance."""

    (tmp_path / "helpers.py").write_text(
        "def imported_reader(path):\n    return path.read_text()\n", encoding="utf-8"
    )
    source = (
        "from pathlib import Path\nfrom helpers import imported_reader\n\n"
        "def local_reader(path):\n    return path.read_text()\n\n"
        "def test_local_positional() -> None:\n    local_reader(Path('docs/local.md'))\n\n"
        "def test_imported_keyword() -> None:\n"
        "    imported_reader(path=Path('docs/imported.md'))\n\n"
        "def test_non_docs() -> None:\n    local_reader(Path('config/local.md'))\n"
    )
    assert _docs_reading_test_modules(source, repository_root=tmp_path) == {
        "test_imported_keyword",
        "test_local_positional",
    }
    ambiguous = (
        source + "\ndef test_ambiguous(name) -> None:\n    local_reader(Path('docs', name))\n"
    )
    with pytest.raises(AssertionError, match="helper argument binding"):
        _docs_reading_test_modules(ambiguous, repository_root=tmp_path)


@pytest.mark.docs_ci
def test_docs_governance_detects_unbound_pathlib_readers() -> None:
    """Unbound pathlib reader calls retain their explicit receiver provenance."""

    source = (
        "from pathlib import Path\n\n"
        "def test_text() -> None:\n    Path.read_text(Path('docs/text.md'))\n\n"
        "def test_bytes() -> None:\n    Path.read_bytes(Path('docs/bytes.md'))\n\n"
        "def test_open() -> None:\n    Path.open(Path('docs/open.md')).read()\n\n"
        "def test_non_docs() -> None:\n    Path.read_text(Path('config/plain.md'))\n"
    )
    assert _docs_reading_test_modules(source) == {"test_bytes", "test_open", "test_text"}
    with pytest.raises(AssertionError, match="cannot prove|ambiguous"):
        _docs_reading_test_modules(
            "from pathlib import Path\n\ndef test_dynamic(name) -> None:\n"
            "    Path.read_text(Path('docs', name))\n"
        )


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "call",
    (
        "Path.read_text()",
        "Path.read_bytes()",
        "Path.open()",
        "Path.read_text(*paths)",
        "Path.read_bytes(*paths)",
        "Path.open(*paths)",
    ),
)
def test_docs_governance_rejects_unbound_pathlib_readers_without_an_explicit_receiver(
    call: str,
) -> None:
    """Unbound pathlib readers require one statically inspectable receiver argument."""

    source = f"from pathlib import Path\n\ndef test_unbound() -> None:\n    {call}\n"
    if "*paths" in call:
        source = "from pathlib import Path\npaths = [Path('docs/guide.md')]\n\n" + (
            f"def test_unbound() -> None:\n    {call}\n"
        )
    with pytest.raises(AssertionError, match="unbound pathlib reader receiver is ambiguous"):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
def test_docs_governance_allows_variadic_helper_unknown_keywords_for_non_docs_paths() -> None:
    """A ``**kwargs`` helper binding does not overmark a proven non-doc receiver."""

    source = (
        "from pathlib import Path\n\n"
        "def helper(path, **kwargs):\n    return path.read_text()\n\n"
        "def test_non_docs() -> None:\n"
        "    helper(Path('config/guide.md'), extra=Path('docs/guide.md'))\n"
    )
    assert _docs_reading_test_modules(source) == set()


@pytest.mark.docs_ci
def test_docs_governance_audits_locally_assigned_collected_callables() -> None:
    """Collected local aliases share their function provenance and marker audit."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.docs\ndef verify_doc() -> None:\n    Path('docs/alias.md').read_text()\n\n"
        "def verify_config() -> None:\n    Path('config/alias.md').read_text()\n\n"
        "test_doc = verify_doc\ntest_config = verify_config\n"
    )
    tree = ast.parse(source)
    assert _docs_reading_test_modules(source) == {"test_doc"}
    assert _marked_docs_test_prefixes(tree) == {"test_doc"}
    _assert_exact_docs_markers(tree, {"test_doc"}, "alias.py")
    with pytest.raises(AssertionError, match="collected local callable alias"):
        _docs_reading_test_modules(source + "test_doc = verify_config\n", filename="alias.py")


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_imported_non_function_receivers(tmp_path: Path) -> None:
    """Imported classes and values cannot hide direct docs reads."""

    (tmp_path / "helpers.py").write_text(
        "from pathlib import Path\n\nclass Reader: pass\n"
        "DOCS_PATH = Path('docs/imported.md')\nCONFIG_PATH = Path('config/imported.md')\n"
    )
    assert (
        _docs_reading_test_modules(
            "from helpers import Reader\n\ndef test_class() -> None:\n    Reader()\n",
            repository_root=tmp_path,
        )
        == set()
    )
    assert _docs_reading_test_modules(
        "from helpers import DOCS_PATH\n\ndef test_path() -> None:\n    DOCS_PATH.read_text()\n",
        repository_root=tmp_path,
    ) == {"test_path"}
    assert (
        _docs_reading_test_modules(
            "from helpers import CONFIG_PATH\n\n"
            "def test_non_docs() -> None:\n    assert CONFIG_PATH\n",
            repository_root=tmp_path,
        )
        == set()
    )
    (tmp_path / "helpers.py").write_text("DOCS_PATH = build_path()\n")
    with pytest.raises(AssertionError, match="imported value provenance"):
        _docs_reading_test_modules(
            "from helpers import DOCS_PATH\n\n"
            "def test_path() -> None:\n    DOCS_PATH.read_text()\n",
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_repository_imported_class_method_calls(
    tmp_path: Path,
) -> None:
    """Imported repository classes cannot hide docs reads behind their methods."""
    (tmp_path / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "class Docs:\n"
        "    @staticmethod\n"
        "    def load() -> str:\n"
        "        return Path('docs/imported-class.md').read_text()\n\n"
        "class Config:\n"
        "    @staticmethod\n"
        "    def load() -> str:\n"
        "        return Path('config/imported-class.md').read_text()\n"
    )
    for class_name in ("Docs", "Config"):
        source = (
            f"from helpers import {class_name}\n\n"
            f"def test_class_method() -> None:\n    assert {class_name}.load()\n"
        )
        with pytest.raises(AssertionError, match="imported class method call provenance"):
            _docs_reading_test_modules(source, repository_root=tmp_path)


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_repository_imported_class_instances(
    tmp_path: Path,
) -> None:
    """Chained and assigned imported-class instances cannot hide docs reads."""
    (tmp_path / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "class Docs:\n"
        "    def __init__(self, label: str = '') -> None:\n"
        "        self.label = label\n\n"
        "    async def load(self) -> str:\n"
        "        return Path('docs/imported-instance.md').read_text()\n\n"
        "class Config:\n"
        "    def load(self) -> str:\n"
        "        return Path('config/imported-instance.md').read_text()\n"
    )
    sources = (
        "from helpers import Docs\n\n"
        "async def test_chained() -> None:\n    assert await Docs('x').load()\n",
        "from helpers import Docs\n\n"
        "def test_assigned() -> None:\n"
        "    instance = Docs('x')\n"
        "    alias = instance\n"
        "    assert alias.load()\n",
        "from helpers import Docs\n\n"
        "def test_branch(enabled: bool) -> None:\n"
        "    if enabled:\n"
        "        instance = Docs()\n"
        "    else:\n"
        "        instance = object()\n"
        "    assert instance.load()\n",
        "from helpers import Config\n\n"
        "def test_second_class() -> None:\n    assert Config().load()\n",
    )
    for source in sources:
        with pytest.raises(AssertionError, match="imported class instance method call provenance"):
            _docs_reading_test_modules(source, repository_root=tmp_path)


@pytest.mark.docs_ci
def test_docs_governance_allows_reassigned_non_class_instances_and_rejects_ambiguous_ones(
    tmp_path: Path,
) -> None:
    """Sequentially cleared provenance is safe, while dynamic imported-class flow is not."""
    (tmp_path / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "class Docs:\n"
        "    def load(self) -> str:\n"
        "        return Path('docs/ambiguous-instance.md').read_text()\n"
    )
    non_class_source = (
        "from helpers import Docs\n\n"
        "def test_non_class() -> None:\n"
        "    instance = Docs()\n"
        "    instance = object()\n"
        "    assert instance.__class__\n"
    )
    assert _docs_reading_test_modules(non_class_source, repository_root=tmp_path) == set()
    ambiguous_source = (
        "from helpers import Docs\n\n"
        "def test_ambiguous() -> None:\n"
        "    instance = construct(Docs)\n"
        "    assert instance.load()\n"
    )
    with pytest.raises(AssertionError, match="imported class instance provenance is ambiguous"):
        _docs_reading_test_modules(ambiguous_source, repository_root=tmp_path)


@pytest.mark.docs_ci
def test_docs_governance_tracks_bound_reader_method_aliases() -> None:
    """Bound reader aliases preserve docs, non-docs, branch, and async provenance."""
    source = (
        "from pathlib import Path\n\n"
        "def test_docs_alias() -> None:\n"
        "    reader = Path('docs/alias.md').read_text\n"
        "    assert reader()\n\n"
        "def test_non_docs_alias() -> None:\n"
        "    path = Path('config/alias.md')\n"
        "    reader = path.read_text\n"
        "    assert reader()\n\n"
        "def test_reassigned_alias() -> None:\n"
        "    reader = Path('docs/first.md').read_text\n"
        "    reader = Path('config/second.md').read_text\n"
        "    assert reader()\n\n"
        "async def test_async_alias() -> None:\n"
        "    reader = Path('docs/async.md').read_bytes\n"
        "    assert reader()\n\n"
        "def test_branch_alias(enabled: bool) -> None:\n"
        "    if enabled:\n"
        "        reader = Path('docs/branch.md').read_text\n"
        "    else:\n"
        "        reader = Path('config/branch.md').read_text\n"
        "    assert reader()\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_async_alias",
        "test_branch_alias",
        "test_docs_alias",
    }


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_dynamic_bound_reader_alias() -> None:
    """An alias of a dynamic reader method cannot suppress marker enforcement."""
    source = (
        "def test_dynamic_alias() -> None:\n"
        "    reader = get_dynamic_reader().read_text\n"
        "    assert reader()\n"
    )
    with pytest.raises(AssertionError, match="bound reader alias `reader` is ambiguous"):
        _docs_reading_test_modules(source, filename="dynamic_reader_alias.py")


@pytest.mark.docs_ci
def test_docs_governance_follows_repository_local_module_import_aliases(tmp_path: Path) -> None:
    """Local module aliases preserve callable and read-receiver provenance."""

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "def read_docs() -> str:\n    return Path('docs/aliased.md').read_text()\n\n"
        "DOCS_PATH = Path('docs/value.md')\nCONFIG_PATH = Path('config/value.md')\n"
    )
    source = (
        "import tests.helpers as helpers\n\n"
        "def test_call() -> None:\n    assert helpers.read_docs()\n\n"
        "def test_value() -> None:\n    helpers.DOCS_PATH.read_text()\n\n"
        "def test_non_docs() -> None:\n    helpers.CONFIG_PATH.read_text()\n"
    )
    assert _docs_reading_test_modules(
        source, filename=str(tests_root / "test_alias.py"), repository_root=tmp_path
    ) == {"test_call", "test_value"}
    (tests_root / "helpers.py").write_text(
        "from pathlib import Path\nDOCS_PATH = Path('docs') / name\n"
    )
    with pytest.raises(AssertionError, match="imported module value provenance"):
        _docs_reading_test_modules(
            "import tests.helpers as helpers\n\n"
            "def test_value():\n    helpers.DOCS_PATH.read_text()\n",
            filename=str(tests_root / "test_alias.py"),
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_rejects_multiple_local_import_roots(
    tmp_path: Path,
) -> None:
    """Competing importer-relative and repository-root modules fail closed."""

    (tmp_path / "tests" / "package").mkdir(parents=True)
    (tmp_path / "tests" / "package" / "helpers.py").write_text(
        "def docs_reader():\n    return ''\n"
    )
    source = (
        "from package.helpers import docs_reader\n\ndef test_reader():\n    assert docs_reader()\n"
    )
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "helpers.py").write_text("def docs_reader():\n    return ''\n")
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(
            source, filename=str(tmp_path / "tests" / "test_import.py"), repository_root=tmp_path
        )


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("import_line", "receiver"),
    [
        pytest.param("import helpers", "helpers", id="plain-module-import"),
        pytest.param(
            "import helpers as local_helpers", "local_helpers", id="aliased-module-import"
        ),
    ],
)
def test_docs_governance_rejects_ambiguous_local_module_import_aliases(
    tmp_path: Path, import_line: str, receiver: str
) -> None:
    """Plain and aliased imports reject competing confined local modules."""

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "helpers.py").write_text("def docs_reader() -> str:\n    return ''\n")
    (tmp_path / "helpers.py").write_text("def docs_reader() -> str:\n    return ''\n")
    source = f"{import_line}\n\ndef test_reader() -> None:\n    assert {receiver}.docs_reader()\n"
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(
            source, filename=str(tests_root / "test_import.py"), repository_root=tmp_path
        )


@pytest.mark.docs_ci
def test_docs_governance_rejects_wildcard_and_reimport_local_imports(tmp_path: Path) -> None:
    """Wildcard and duplicate imports fail before bare calls can evade provenance."""

    (tmp_path / "helpers.py").write_text("def docs_reader() -> str:\n    return ''\n")
    with pytest.raises(AssertionError, match="wildcard repository-local import provenance"):
        _docs_reading_test_modules(
            "from helpers import *\n\ndef test_reader() -> None:\n    docs_reader()\n",
            repository_root=tmp_path,
        )
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(
            "from helpers import docs_reader\nfrom helpers import docs_reader\n\n"
            "def test_reader() -> None:\n    docs_reader()\n",
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_tracks_literal_fixture_activation() -> None:
    """Autouse and literal usefixtures edges select only their affected tests."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "class TestAutouse:\n"
        "    @pytest.fixture(autouse=True)\n"
        "    def docs_fixture(self) -> str:\n"
        "        return Path('docs/autouse.md').read_text()\n\n"
        "    def test_autouse(self) -> None:\n        assert True\n\n"
        "def test_unaffected() -> None:\n    assert True\n\n"
        "@pytest.fixture\n"
        "def marked_fixture() -> str:\n"
        "    return Path('docs/marked.md').read_text()\n\n"
        "@pytest.mark.usefixtures('marked_fixture')\n"
        "def test_marked() -> None:\n    assert True\n\n"
        "class TestClassMarked:\n"
        "    pytestmark = pytest.mark.usefixtures('marked_fixture')\n\n"
        "    def test_class_marked(self) -> None:\n        assert True\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestAutouse::test_autouse",
        "TestClassMarked::test_class_marked",
        "test_marked",
    }
    with pytest.raises(AssertionError, match="autouse"):
        _docs_reading_test_modules(
            source.replace("autouse=True", "autouse=enabled"), filename="dynamic_autouse.py"
        )
    with pytest.raises(AssertionError, match="usefixtures"):
        _docs_reading_test_modules(
            source.replace("'marked_fixture'", "fixture_name"), filename="dynamic_usefixtures.py"
        )


@pytest.mark.docs_ci
def test_docs_governance_rejects_docs_stress_overlap() -> None:
    """A docs reader cannot be excluded from the fast lane by ``stress``."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.docs\n@pytest.mark.stress\n"
        "def test_reader() -> None:\n    Path('docs/stress.md').read_text()\n"
    )
    tree = ast.parse(source)
    readers = _docs_reading_test_modules(source)
    with pytest.raises(AssertionError, match="cannot carry pytest.mark.stress"):
        _assert_exact_docs_markers(tree, readers, "stress_overlap.py")


@pytest.mark.docs_ci
def test_docs_governance_rejects_docs_provenance_in_nested_conftests(tmp_path: Path) -> None:
    """Nested conftest readers, returns, and ambiguous paths cannot evade markers."""

    tests_root = tmp_path / "tests"
    nested = tests_root / "nested"
    nested.mkdir(parents=True)
    (tests_root / "conftest.py").write_text("def helper() -> int:\n    return 1\n")
    (nested / "conftest.py").write_text(
        "from pathlib import Path\n\ndef helper() -> str:\n"
        '    return Path("docs/hidden.md").read_text()\n'
    )
    assert [path.relative_to(tests_root).as_posix() for path in _conftest_paths(tests_root)] == [
        "conftest.py",
        "nested/conftest.py",
    ]
    with pytest.raises(AssertionError, match="nested/conftest.py: conftest exposes"):
        _assert_conftests_docs_free(tests_root)


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_at_call_depth_but_not_cycles() -> None:
    """Unvisited over-bound edges fail while cycles terminate without a false reader."""

    deep = "\n\n".join(
        [
            *(
                f"def helper_{index}() -> str:\n    return helper_{index + 1}()"
                for index in range(9)
            ),
            'def helper_9() -> str:\n    return Path("docs/deep.md").read_text()',
            "def test_deep() -> None:\n    assert helper_0()",
        ]
    )
    with pytest.raises(AssertionError, match="exceeded the bounded"):
        _docs_reading_test_modules("from pathlib import Path\n\n" + deep)
    cycle = (
        "def left() -> str:\n    return right()\n\n"
        "def right() -> str:\n    return left()\n\n"
        "def test_cycle() -> None:\n    assert left()\n"
    )
    assert _docs_reading_test_modules(cycle) == set()


@pytest.mark.docs_ci
def test_docs_governance_call_depth_last_admitted_and_first_exhausted() -> None:
    """Seven total call edges are admitted; the eighth fails closed."""

    admitted = "\n\n".join(
        [
            *(f"def helper_{index}():\n    return helper_{index + 1}()" for index in range(6)),
            "def helper_6():\n    return Path('docs/bound.md').read_text()",
            "def test_admitted():\n    assert helper_0()",
        ]
    )
    assert _docs_reading_test_modules("from pathlib import Path\n\n" + admitted) == {
        "test_admitted"
    }
    exhausted = "\n\n".join(
        [
            *(f"def helper_{index}():\n    return helper_{index + 1}()" for index in range(7)),
            "def helper_7():\n    return 1",
            "def test_exhausted():\n    assert helper_0()",
        ]
    )
    with pytest.raises(AssertionError, match="exceeded the bounded"):
        _docs_reading_test_modules("from pathlib import Path\n\n" + exhausted)

    with pytest.raises(AssertionError, match="exceeded the bounded"):
        _docs_reading_test_modules(exhausted.replace("return 1", "return 2"))


@pytest.mark.docs_ci
def test_docs_governance_binds_test_discovery_to_pytest_configuration() -> None:
    """The path self-audit fails closed when pytest collection settings drift."""

    configuration = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    options = cast(dict[str, object], configuration["tool"]["pytest"]["ini_options"])
    _assert_default_pytest_collection_options(options)
    with pytest.raises(AssertionError, match="python_files"):
        _assert_default_pytest_collection_options(
            {"testpaths": ["tests"], "python_files": ["*_spec.py"]}
        )
    with pytest.raises(AssertionError, match="testpaths"):
        _assert_default_pytest_collection_options({"testpaths": ["integration"]})
    with pytest.raises(AssertionError, match="python_functions"):
        _assert_default_pytest_collection_options(
            {"testpaths": ["tests"], "python_functions": ["test*"]}
        )


@pytest.mark.docs_ci
def test_docs_governance_detects_a_hidden_unmarked_markdown_read() -> None:
    """The AST rule catches a direct Markdown read even without a path variable name."""

    source = (
        "from pathlib import Path\n\n"
        "def test_hidden() -> None:\n"
        '    Path("docs/hidden.md").read_text()\n'
    )
    assert _docs_reading_test_modules(source) == {"test_hidden"}
    assert not _module_has_unsafe_pytestmark(ast.parse(source))


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("pytestmark = pytest.mark.docs\n", True, id="scalar-docs"),
        pytest.param(
            "pytestmark = [pytest.mark.docs, pytest.mark.slow]\n",
            True,
            id="list-docs",
        ),
        pytest.param(
            "pytestmark = (pytest.mark.slow, pytest.mark.docs)\n",
            True,
            id="tuple-docs",
        ),
        pytest.param(
            "pytestmark = {pytest.mark.docs, pytest.mark.slow}\n",
            True,
            id="set-docs",
        ),
        pytest.param("pytestmark = pytest.mark.slow\n", False, id="scalar-non-docs"),
        pytest.param(
            "pytestmark = pytest.mark.skipif(condition, reason='because')\n",
            False,
            id="non-docs-marker-call",
        ),
        pytest.param(
            "pytestmark = [pytest.mark.slow, pytest.mark.serial]\n",
            False,
            id="unrelated-list",
        ),
        pytest.param("pytestmark = module_marks\n", True, id="dynamic-name"),
        pytest.param("pytestmark = [*module_marks]\n", True, id="starred-container"),
        pytest.param(
            "pytestmark = [pytest.mark.slow] + module_marks\n",
            True,
            id="concatenation",
        ),
    ],
)
def test_docs_governance_rejects_unsafe_module_docs_markers(source: str, expected: bool) -> None:
    """Only statically proven docs-free module marks pass the retired-mark audit."""

    assert _module_has_unsafe_pytestmark(ast.parse(source)) is expected


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\nimport pytest\npytestmark = pytest.mark.stress\n\n"
        "@pytest.mark.docs\ndef test_reader():\n    Path('docs/a.md').read_text()\n",
        "from pathlib import Path\nimport pytest\n\nclass TestReader:\n"
        "    pytestmark = pytest.mark.stress\n    @pytest.mark.docs\n"
        "    def test_reader(self):\n        Path('docs/a.md').read_text()\n",
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.stress\nclass TestReader:\n"
        "    @pytest.mark.docs\n    def test_reader(self):\n"
        "        Path('docs/a.md').read_text()\n",
        "from pathlib import Path\nimport pytest\n\n@pytest.mark.docs\n@pytest.mark.stress\n"
        "def test_reader():\n    Path('docs/a.md').read_text()\n",
    ],
)
def test_docs_governance_rejects_effective_stress_inheritance(source: str) -> None:
    """Docs readers cannot be excluded by module, class, or function stress marks."""

    tree = ast.parse(source)
    with pytest.raises(AssertionError, match="cannot carry pytest.mark.stress"):
        _assert_exact_docs_markers(tree, _docs_reading_test_modules(source), "stress_reader.py")


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'from pathlib import Path\n\ndef test_a() -> None:\n    Path("docs") / "note.md"\n',
            set[str](),
            id="path-slash-joined",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    p = Path("root") / "docs" / "note.md"\n'
            "    p.read_text()\n",
            {"test_a"},
            id="path-slash-joined-and-read",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    p = Path("root") / "config" / "note.md"\n'
            "    p.read_text()\n",
            set[str](),
            id="path-slash-joined-negative",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    p = Path("root").joinpath("docs", "note.md")\n'
            "    p.read_text()\n",
            {"test_a"},
            id="joinpath",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    p = Path("root").joinpath("config", "note.md")\n'
            "    p.read_text()\n",
            set[str](),
            id="joinpath-negative",
        ),
        pytest.param(
            "import os\n\n"
            "def test_a() -> None:\n"
            '    stem = "note"\n'
            '    path = os.path.join("docs", stem + ".md")\n'
            "    open(path).read()\n",
            {"test_a"},
            id="os-path-join",
        ),
        pytest.param(
            "import os\n\n"
            "def test_a() -> None:\n"
            '    stem = "note"\n'
            '    path = os.path.join("config", stem + ".md")\n'
            "    open(path).read()\n",
            set[str](),
            id="os-path-join-negative",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    stem = "note"\n'
            '    p = Path(f"docs/{stem}.md")\n'
            "    p.read_text()\n",
            {"test_a"},
            id="f-string",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    p = Path("docs/{}.md".format("note"))\n'
            "    p.read_text()\n",
            {"test_a"},
            id="str-format",
        ),
        pytest.param(
            "def test_a() -> None:\n"
            '    with open("docs/note.md") as handle:\n'
            "        handle.read()\n",
            {"test_a"},
            id="builtin-open",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    with Path("docs/note.md").open() as handle:\n'
            "        handle.read()\n",
            {"test_a"},
            id="path-open-method",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    for entry in Path("docs").rglob("*.md"):\n'
            "        entry.read_text()\n",
            {"test_a"},
            id="rglob-star-md",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    root = Path("repo") / "docs"\n'
            '    for entry in root.rglob("*.md"):\n'
            "        entry.read_text()\n",
            {"test_a"},
            id="rglob-star-md-built-with-slash",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    texts = [entry.read_text() for entry in Path("docs").rglob("*.md")]\n'
            "    assert texts\n",
            {"test_a"},
            id="list-comprehension-docs-rglob",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    assert any(entry.read_text() for entry in Path("docs").rglob("*.md"))\n',
            {"test_a"},
            id="generator-comprehension-docs-rglob",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    texts = [entry.read_text() for entry in Path("config").glob("*.md")]\n'
            "    assert texts\n",
            set[str](),
            id="list-comprehension-non-docs-negative",
        ),
        pytest.param(
            "from pathlib import Path\n\n"
            "def test_a() -> None:\n"
            '    for entry in Path("config").glob("*.md"):\n'
            "        entry.read_text()\n",
            set[str](),
            id="glob-non-docs-negative",
        ),
    ],
)
def test_docs_governance_detects_non_literal_construction(source: str, expected: set[str]) -> None:
    """Non-literal docs-path construction is detected by folded-constant proof."""

    assert _docs_reading_test_modules(source) == expected


@pytest.mark.docs_ci
def test_docs_governance_accepts_builtin_open_file_keyword() -> None:
    """The unique builtin ``file=`` receiver is equivalent to positional input."""

    source = (
        "def test_keyword() -> None:\n"
        "    with open(file='docs/keyword.md', encoding='utf-8') as handle:\n"
        "        assert handle.read()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_keyword"}


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("call", "message"),
    [
        pytest.param(
            "open('docs/a.md', file='docs/b.md')",
            "conflicting positional",
            id="conflicting-receivers",
        ),
        pytest.param("open(**kwargs)", "dynamic keyword", id="dynamic-keywords"),
        pytest.param("open()", "no statically unique", id="no-receiver"),
        pytest.param("open(*args)", "dynamic positional", id="dynamic-positional"),
    ],
)
def test_docs_governance_fails_closed_on_ambiguous_builtin_open_shapes(
    call: str, message: str
) -> None:
    """Duplicate, conflicting, and dynamic builtin-open shapes cannot hide readers."""

    source = f"def test_open() -> None:\n    {call}\n"
    with pytest.raises(AssertionError, match=message):
        _docs_reading_test_modules(source)

    duplicate = ast.Call(
        func=ast.Name(id="open"),
        args=[],
        keywords=[
            ast.keyword(arg="file", value=ast.Constant(value="docs/a.md")),
            ast.keyword(arg="file", value=ast.Constant(value="docs/b.md")),
        ],
    )
    with pytest.raises(AssertionError, match="duplicate"):
        _builtin_open_target(duplicate)


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "call",
    [
        "Path('docs').glob('*.md')",
        "Path('docs').rglob('*.rst')",
        "Path('docs').rglob(pattern)",
    ],
)
def test_docs_governance_fails_closed_on_ambiguous_docs_rooted_globs(call: str) -> None:
    """Only the exact recursive Markdown glob may establish docs provenance."""

    source = f"from pathlib import Path\n\ndef test_glob() -> None:\n    list({call})\n"
    with pytest.raises(AssertionError, match="docs-rooted glob"):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
def test_docs_governance_tracks_bounded_parametrized_path_sources() -> None:
    """Literal and uniquely named parametrization values classify path receivers."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "DOCS = (Path('docs/one.md'),)\n"
        "SCRIPTS = (Path('scripts/one.py'),)\n\n"
        "@pytest.mark.parametrize('path', DOCS)\n"
        "def test_docs(path: Path) -> None:\n    path.read_text()\n\n"
        "@pytest.mark.parametrize('path', SCRIPTS)\n"
        "def test_non_docs(path: Path) -> None:\n    path.read_text()\n\n"
        "@pytest.mark.parametrize('path', unknown)\n"
        "def test_unknown(path: Path) -> None:\n    path.read_text()\n"
    )
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(source)
    assert _docs_reading_test_modules(source.rsplit("@pytest.mark.parametrize", 1)[0]) == {
        "test_docs"
    }


@pytest.mark.docs_ci
def test_docs_governance_traces_indirect_fixture_params() -> None:
    """Literal indirect fixture params reach fixtures that consume ``request.param``."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\ndef path(request):\n    request.param.read_text()\n\n"
        "@pytest.mark.parametrize('path', [Path('docs/indirect.md')], indirect=True)\n"
        "def test_docs(path):\n    assert path\n\n"
        "@pytest.mark.parametrize('path', [Path('config/indirect.md')], indirect=['path'])\n"
        "def test_non_reader(path):\n    assert path\n"
    )
    assert _docs_reading_test_modules(source) == {"test_docs"}
    with pytest.raises(AssertionError, match="indirect fixture parameter provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("indirect=True", "indirect=dynamic_indirect"))


@pytest.mark.docs_ci
def test_docs_governance_traces_docs_iterdir_and_explicit_test_flags() -> None:
    """Docs ``iterdir`` and explicit pytest collection cannot hide readers."""

    assert _docs_reading_test_modules(
        "from pathlib import Path\n\ndef test_iterdir():\n"
        "    [entry.read_text() for entry in Path('docs').iterdir()]\n"
    ) == {"test_iterdir"}
    with pytest.raises(AssertionError, match="explicit __test__ callable provenance is ambiguous"):
        _docs_reading_test_modules(
            "from pathlib import Path\n\ndef reader():\n"
            "    Path('docs/explicit.md').read_text()\n\nreader.__test__ = True\n"
        )


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_rebound_parametrization_sources() -> None:
    """Only uniquely named module sources may feed parametrized read receivers."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "DOCS = [Path('docs/a.md')]\nDOCS = [Path('docs/b.md')]\n"
        "(ignored, ) = [Path('docs/ignored.md')]\n\n"
        "@pytest.mark.parametrize('path', DOCS)\n"
        "def test_reader(path: Path) -> None:\n    path.read_text()\n"
    )
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "argnames",
    ["('path', 'label')", "['path', 'label']", "'path,label'"],
)
def test_docs_governance_fails_closed_on_multi_value_parametrized_receivers(
    argnames: str,
) -> None:
    """Multi-value parametrization cannot silently omit a path receiver."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        f"@pytest.mark.parametrize({argnames}, [(Path('docs/a.md'), 'x')])\n"
        "def test_reader(path: Path, label: str) -> None:\n"
        "    path.read_text()\n"
    )
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "argnames_source",
    ["ARGNAMES", "('path', DYNAMIC_NAME)"],
)
def test_docs_governance_fails_closed_on_dynamic_parametrized_argnames(
    argnames_source: str,
) -> None:
    """Dynamic or unsupported argname forms cannot omit a read receiver."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "ARGNAMES = 'path'\nDYNAMIC_NAME = 'label'\n\n"
        f"@pytest.mark.parametrize({argnames_source}, [Path('docs/a.md')])\n"
        "def test_reader(path: Path) -> None:\n"
        "    path.read_text()\n"
    )
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
def test_docs_governance_propagates_staged_docs_root_aliases() -> None:
    """Partial docs roots remain known through slash and joinpath composition."""

    source = (
        "from pathlib import Path\n\n"
        "def test_slash() -> None:\n"
        '    root = Path("docs")\n'
        '    path = root / "guide.md"\n'
        "    path.read_text()\n\n"
        "def test_joinpath() -> None:\n"
        '    root = Path("docs")\n'
        '    path = root.joinpath("guide.md")\n'
        "    path.read_text()\n\n"
        "def test_non_docs() -> None:\n"
        '    root = Path("config")\n'
        '    path = root / "guide.md"\n'
        "    path.read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_joinpath", "test_slash"}


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "branches",
    [
        "if enabled:\n        path = Path('docs/branch.md')\n"
        "    else:\n        path = Path('config/branch.md')",
        "if enabled:\n        path = Path('config/branch.md')\n"
        "    else:\n        path = Path('docs/branch.md')",
    ],
)
def test_docs_governance_unions_docs_aliases_across_if_branches(branches: str) -> None:
    """A docs alias from either branch remains sensitive at the later read."""

    source = (
        "from pathlib import Path\n\n"
        "def test_branch(enabled: bool) -> None:\n"
        f"    {branches}\n"
        "    path.read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_branch"}
    non_docs = (
        "from pathlib import Path\n\n"
        "def test_non_docs() -> None:\n"
        "    path = Path('config/only.md')\n"
        "    path.read_text()\n"
    )
    assert _docs_reading_test_modules(non_docs) == set()


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_ambiguous_staged_docs_root_alias() -> None:
    """A partial docs alias without a proven Markdown suffix stays fail-closed."""

    source = (
        "from pathlib import Path\n\n"
        "def test_ambiguous() -> None:\n"
        '    root = Path("docs")\n'
        "    path = root / filename\n"
        "    path.read_text()\n"
    )
    with pytest.raises(AssertionError, match="no provable '.md' segment"):
        _docs_reading_test_modules(source, filename="staged_alias.py")


@pytest.mark.docs_ci
def test_docs_governance_follows_a_same_module_helper_call() -> None:
    """A test reaching a docs read only through a same-module helper is caught."""

    source = (
        "from pathlib import Path\n\n"
        "def _load() -> str:\n"
        '    return Path("docs/note.md").read_text()\n\n'
        "def test_via_helper() -> None:\n"
        "    text = _load()\n"
        "    assert text\n\n"
        "def _no_read() -> int:\n"
        "    return 1\n\n"
        "def test_via_non_reading_helper() -> None:\n"
        "    assert _no_read() == 1\n"
    )
    assert _docs_reading_test_modules(source) == {"test_via_helper"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_docs_paths_returned_by_helpers_and_fixtures() -> None:
    """Returned docs paths count only when a consumer subsequently reads them."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "def path_leaf() -> Path:\n"
        '    return Path("docs/returned.md")\n\n'
        "async def path_middle() -> Path:\n"
        "    return path_leaf()\n\n"
        "def test_helper_read() -> None:\n"
        "    path = path_leaf()\n"
        "    path.read_text()\n\n"
        "async def test_async_read() -> None:\n"
        "    path = await path_middle()\n"
        "    path.read_bytes()\n\n"
        "def test_direct_return_read() -> None:\n"
        "    path_leaf().read_text()\n\n"
        "def test_receives_without_read() -> None:\n"
        "    assert path_leaf()\n\n"
        "@pytest.fixture\n"
        "def docs_path() -> Path:\n"
        '    return Path("docs/fixture-return.md")\n\n'
        "def test_fixture_read(docs_path: Path) -> None:\n"
        "    docs_path.open()\n\n"
        "def test_fixture_without_read(docs_path: Path) -> None:\n"
        "    assert docs_path\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_async_read",
        "test_direct_return_read",
        "test_fixture_read",
        "test_helper_read",
    }


@pytest.mark.docs_ci
def test_docs_governance_tracks_docs_paths_returned_by_local_helpers() -> None:
    """Invoked local helpers retain docs-path provenance at direct read receivers."""

    source = (
        "from pathlib import Path\n\n"
        "def test_local_path() -> None:\n"
        "    def local_path() -> Path:\n"
        "        return Path('docs/local-return.md')\n\n"
        "    local_path().read_text()\n\n"
        "def test_local_non_docs_path() -> None:\n"
        "    def local_path() -> Path:\n"
        "        return Path('config/local-return.md')\n\n"
        "    local_path().read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_local_path"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_open_on_a_returned_path() -> None:
    """A returned docs path is also traced through builtin ``open`` receivers."""

    source = (
        "from pathlib import Path\n\n"
        "def docs_path() -> Path:\n    return Path('docs/open.md')\n\n"
        "def config_path() -> Path:\n    return Path('config/open.md')\n\n"
        "def test_docs() -> None:\n    open(docs_path())\n\n"
        "def test_non_docs() -> None:\n    open(config_path())\n"
    )
    assert _docs_reading_test_modules(source) == {"test_docs"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_exact_qualified_open_calls() -> None:
    """Literal ``io.open`` and ``builtins.open`` reads use builtin-open provenance rules."""

    source = (
        "import builtins\nimport io\n\n"
        "def test_io_positional() -> None:\n    io.open('docs/io.md')\n\n"
        "def test_builtins_keyword() -> None:\n    builtins.open(file='docs/builtins.md')\n\n"
        "def test_non_docs() -> None:\n    io.open('config/no.md')\n"
    )
    assert _docs_reading_test_modules(source) == {"test_builtins_keyword", "test_io_positional"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_unambiguous_builtin_open_aliases() -> None:
    """Builtin-open aliases retain docs provenance and sequential rebinds clear it."""

    source = (
        "import builtins\nfrom builtins import open as imported_open\n\n"
        "def test_plain_alias() -> None:\n"
        "    reader = open\n    reader('docs/plain.md')\n\n"
        "def test_qualified_alias() -> None:\n"
        "    reader = builtins.open\n    reader('docs/qualified.md')\n\n"
        "def test_imported_alias() -> None:\n"
        "    imported_open('docs/imported.md')\n\n"
        "def test_non_docs() -> None:\n"
        "    reader = open\n    reader('config/plain.md')\n\n"
        "def test_reassigned_non_reader() -> None:\n"
        "    reader = open\n    reader = print\n    reader('docs/not-read.md')\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_imported_alias",
        "test_plain_alias",
        "test_qualified_alias",
    }


@pytest.mark.docs_ci
def test_docs_governance_rejects_ambiguous_builtin_open_aliases() -> None:
    """A conditional builtin-open rebind cannot silently skip a docs marker."""

    source = (
        "def test_branch(enabled: bool) -> None:\n"
        "    if enabled:\n        reader = open\n"
        "    else:\n        reader = print\n"
        "    reader('docs/branch.md')\n"
    )
    with pytest.raises(AssertionError, match="builtin open alias `reader` is ambiguous"):
        _docs_reading_test_modules(source, filename="open_alias.py")


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "import io as io_module\n\n"
            "def test_alias() -> None:\n    io_module.open('docs/alias.md')\n",
            "aliased qualified open is ambiguous",
        ),
        (
            "import io\n\n"
            "def test_conflict() -> None:\n    io.open('docs/one.md', file='docs/two.md')\n",
            "conflicting positional",
        ),
        (
            "import builtins\n\ndef test_dynamic() -> None:\n    builtins.open(*paths)\n",
            "dynamic positional",
        ),
    ],
)
def test_docs_governance_fails_closed_on_qualified_open_ambiguity(
    source: str, message: str
) -> None:
    """Aliased or malformed qualified open shapes cannot bypass docs governance."""

    with pytest.raises(AssertionError, match=message):
        _docs_reading_test_modules(source, filename="qualified_open.py")


@pytest.mark.docs_ci
def test_docs_governance_follows_a_same_module_fixture() -> None:
    """A test reaching a docs read only through a same-module fixture is caught."""

    source = (
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def runbook() -> str:\n"
        '    return Path("docs/note.md").read_text()\n\n'
        "def test_uses_fixture(runbook: str) -> None:\n"
        "    assert runbook\n\n"
        "@pytest.fixture\n"
        "def unrelated() -> int:\n"
        "    return 1\n\n"
        "def test_uses_unrelated_fixture(unrelated: int) -> None:\n"
        "    assert unrelated == 1\n"
    )
    assert _docs_reading_test_modules(source) == {"test_uses_fixture"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_applicable_xunit_lifecycle_hooks() -> None:
    """Docs reads in pytest xunit hooks reach only their governed collected tests."""

    source = (
        "from pathlib import Path\n\n"
        "def setup_function() -> None:\n    Path('docs/module-hook.md').read_text()\n\n"
        "def test_module_hook() -> None:\n    assert True\n\n"
        "class TestHooks:\n"
        "    def setup_method(self) -> None:\n        Path('docs/method-hook.md').read_text()\n\n"
        "    def test_method_hook(self) -> None:\n        assert True\n"
    )
    assert _docs_reading_test_modules(source) == {"TestHooks::test_method_hook", "test_module_hook"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_literal_request_getfixturevalue_dependencies() -> None:
    """Literal request.getfixturevalue calls add their fixture edge exactly."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\ndef docs_fixture() -> str:\n"
        "    return Path('docs/request.md').read_text()\n\n"
        "@pytest.fixture\ndef plain_fixture() -> int:\n    return 1\n\n"
        "def test_docs(request) -> None:\n    assert request.getfixturevalue('docs_fixture')\n\n"
        "def test_plain(request) -> None:\n"
        "    assert request.getfixturevalue('plain_fixture') == 1\n"
    )
    assert _docs_reading_test_modules(source) == {"test_docs"}
    with pytest.raises(
        AssertionError, match="request.getfixturevalue requires one literal fixture name"
    ):
        _docs_reading_test_modules(source.replace("'plain_fixture'", "fixture_name"))


@pytest.mark.docs_ci
def test_docs_governance_tracks_docs_rooted_parameter_defaults() -> None:
    """Positional and keyword-only defaults retain docs or fail-closed provenance."""

    source = (
        "from pathlib import Path\n\n"
        "def test_positional(path=Path('docs/default.md')) -> None:\n    path.read_text()\n\n"
        "def test_keyword_only(*, path=Path('docs/keyword.md')) -> None:\n    path.read_bytes()\n\n"
        "def test_plain(path=Path('config/default.md')) -> None:\n    path.read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_keyword_only", "test_positional"}
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(
            "from pathlib import Path\n\ndef test_dynamic(path=build_path()) -> None:\n"
            "    path.read_text()\n"
        )


@pytest.mark.docs_ci
def test_docs_governance_recognizes_scoped_pytest_fixture_import_aliases() -> None:
    """A direct pytest.fixture import alias participates in the fixture graph."""

    source = (
        "from pathlib import Path\nfrom pytest import fixture as fx\n\n"
        "@fx\ndef docs_alias() -> str:\n    return Path('docs/alias-fixture.md').read_text()\n\n"
        "def test_alias(docs_alias: str) -> None:\n    assert docs_alias\n"
    )
    assert _docs_reading_test_modules(source) == {"test_alias"}
    with pytest.raises(AssertionError, match="fixture decorator alias `fx` is ambiguous"):
        _docs_reading_test_modules(source.replace("@fx", "fx = object\n\n@fx"))

    module_alias = source.replace(
        "from pytest import fixture as fx", "import pytest as pt"
    ).replace("@fx", "@pt.fixture")
    assert _docs_reading_test_modules(module_alias) == {"test_alias"}
    with pytest.raises(AssertionError, match="pytest module alias `pt` is ambiguous"):
        _docs_reading_test_modules(
            module_alias.replace("@pt.fixture", "pt = object\n\n@pt.fixture")
        )


@pytest.mark.docs_ci
def test_docs_governance_tracks_yielded_fixture_paths_only_when_read() -> None:
    """Yielded docs paths retain provenance without over-marking non-read consumers."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def docs_path() -> Path:\n"
        '    yield Path("docs/yielded.md")\n\n'
        "def test_reads(docs_path: Path) -> None:\n"
        "    docs_path.read_text()\n\n"
        "def test_does_not_read(docs_path: Path) -> None:\n"
        "    assert docs_path\n"
    )
    assert _docs_reading_test_modules(source) == {"test_reads"}


@pytest.mark.docs_ci
def test_docs_governance_tracks_alias_transitive_and_async_yielded_paths() -> None:
    """Yield provenance crosses aliases, fixture dependencies, and async generators."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def leaf():\n    path = Path('docs/alias-yield.md')\n    yield path\n\n"
        "@pytest.fixture\n"
        "def middle(leaf: Path):\n    yield leaf\n\n"
        "@pytest.fixture\n"
        "async def async_leaf():\n    yield Path('docs/async-yield.md')\n\n"
        "def test_alias(leaf: Path):\n    leaf.read_text()\n\n"
        "def test_transitive(middle: Path):\n    middle.read_bytes()\n\n"
        "async def test_async(async_leaf: Path):\n    async_leaf.open()\n\n"
        "def test_leaf_no_read(leaf: Path):\n    assert leaf\n\n"
        "def test_middle_no_read(middle: Path):\n    assert middle\n\n"
        "async def test_async_no_read(async_leaf: Path):\n    assert async_leaf\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_alias",
        "test_async",
        "test_transitive",
    }


@pytest.mark.docs_ci
def test_docs_governance_tracks_fixture_params_request_provenance() -> None:
    """Literal fixture params propagate only to consumers that read the value."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture(params=[Path('docs/param.md')])\n"
        "def direct(request) -> Path:\n    return request.param\n\n"
        "@pytest.fixture(params=[Path('docs/alias.md')])\n"
        "def alias(request):\n    value = request.param\n    yield value\n\n"
        "def test_direct(direct: Path) -> None:\n    direct.read_text()\n\n"
        "def test_alias(alias: Path) -> None:\n    alias.read_bytes()\n\n"
        "def test_no_read(direct: Path) -> None:\n    assert direct\n"
    )
    assert _docs_reading_test_modules(source) == {"test_alias", "test_direct"}


@pytest.mark.docs_ci
@pytest.mark.parametrize("params", ["[Path('config/no.md')]", "unknown", "VALUES"])
def test_docs_governance_handles_non_docs_and_ambiguous_fixture_params(params: str) -> None:
    """Non-doc fixture params pass; unresolved or rebound params fail closed."""

    prefix = (
        "VALUES = [Path('docs/first.md')]\nVALUES = [Path('docs/second.md')]\n\n"
        if params == "VALUES"
        else ""
    )
    source = (
        "from pathlib import Path\nimport pytest\n\n"
        + prefix
        + f"@pytest.fixture(params={params})\n"
        "def value(request) -> Path:\n    return request.param\n\n"
        "def test_value(value: Path) -> None:\n    value.read_text()\n"
    )
    if params.startswith("["):
        assert _docs_reading_test_modules(source) == set()
    else:
        with pytest.raises(AssertionError, match="request.param provenance is ambiguous"):
            _docs_reading_test_modules(source)


@pytest.mark.docs_ci
def test_docs_governance_rejects_unsafe_conftest_fixture_params(tmp_path: Path) -> None:
    """Shared fixture params cannot hide docs or unresolved path provenance."""

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    cases = {
        "docs": "[Path('docs/a.md')]",
        "unknown": "unknown",
        "non_docs": "[Path('config/a.md')]",
    }
    for name, params in cases.items():
        case = tests_root / name
        case.mkdir()
        (case / "conftest.py").write_text(
            "from pathlib import Path\nimport pytest\n\n"
            f"@pytest.fixture(params={params})\n"
            "def value(request):\n    return request.param\n"
        )
    for name in ("docs", "unknown"):
        with pytest.raises(AssertionError, match="conftest"):
            _assert_conftests_docs_free(tests_root / name)
    _assert_conftests_docs_free(tests_root / "non_docs")


@pytest.mark.docs_ci
def test_docs_governance_follows_keyword_only_fixture_parameters() -> None:
    """Keyword-only test and fixture parameters retain their static fixture edges."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def direct_docs() -> str:\n"
        '    return Path("docs/direct.md").read_text()\n\n'
        "def test_keyword_only(*, direct_docs: str) -> None:\n"
        "    assert direct_docs\n\n"
        "@pytest.fixture\n"
        "def docs_leaf() -> str:\n"
        '    return Path("docs/leaf.md").read_text()\n\n'
        "@pytest.fixture\n"
        "def docs_middle(*, docs_leaf: str) -> str:\n"
        "    return docs_leaf\n\n"
        "def test_keyword_only_transitive(*, docs_middle: str) -> None:\n"
        "    assert docs_middle\n\n"
        "@pytest.fixture\n"
        "def plain_leaf() -> int:\n"
        "    return 1\n\n"
        "@pytest.fixture\n"
        "def plain_middle(*, plain_leaf: int) -> int:\n"
        "    return plain_leaf\n\n"
        "def test_keyword_only_plain(*, plain_middle: int) -> None:\n"
        "    assert plain_middle == 1\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_keyword_only",
        "test_keyword_only_transitive",
    }


@pytest.mark.docs_ci
def test_docs_governance_resolves_literal_fixture_name_overrides() -> None:
    """Literal fixture aliases retain direct, transitive, and async reader edges."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture(name='docs_alias')\n"
        "def docs_source() -> str:\n"
        '    return Path("docs/alias.md").read_text()\n\n'
        "@pytest.fixture(name='middle_alias')\n"
        "def docs_middle(*, docs_alias: str) -> str:\n"
        "    return docs_alias\n\n"
        "def test_alias(*, middle_alias: str) -> None:\n"
        "    assert middle_alias\n\n"
        "@pytest.fixture(name='async_alias')\n"
        "async def async_source() -> str:\n"
        '    return Path("docs/async-alias.md").read_text()\n\n'
        "async def test_async_alias(*, async_alias: str) -> None:\n"
        "    assert async_alias\n\n"
        "@pytest.fixture(name='plain_alias')\n"
        "def plain_source() -> int:\n"
        "    return 1\n\n"
        "def test_plain_alias(*, plain_alias: int) -> None:\n"
        "    assert plain_alias == 1\n"
    )
    assert _docs_reading_test_modules(source) == {"test_alias", "test_async_alias"}


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param(
            "import pytest\n\n"
            "@pytest.fixture(name=fixture_name)\n"
            "def fixture() -> int:\n"
            "    return 1\n",
            "non-literal",
            id="dynamic-name",
        ),
        pytest.param(
            "import pytest\n\n@pytest.fixture(name='')\ndef fixture() -> int:\n    return 1\n",
            "empty",
            id="empty-name",
        ),
        pytest.param(
            "import pytest\n\n@pytest.fixture(*options)\ndef fixture() -> int:\n    return 1\n",
            "ambiguous fixture decorator shape",
            id="dynamic-decorator-shape",
        ),
        pytest.param(
            "import pytest\n\n@pytest.fixture(name='first')\n@pytest.fixture(name='second')\n"
            "def fixture() -> int:\n    return 1\n",
            "multiple fixture decorators",
            id="conflicting-name-overrides",
        ),
        pytest.param(
            "import pytest\n\n@pytest.fixture(name='same')\ndef first() -> int:\n    return 1\n\n"
            "@pytest.fixture(name='same')\ndef second() -> int:\n    return 2\n",
            "duplicate name",
            id="duplicate-exposed-name",
        ),
        pytest.param(
            "import pytest\n\n"
            "@pytest.fixture(name='same')\n"
            "def module_fixture() -> int:\n"
            "    return 1\n\n"
            "class TestScoped:\n"
            "    @pytest.fixture(name='same')\n"
            "    def class_fixture(self) -> int:\n"
            "        return 2\n\n"
            "    def test_one(self) -> None:\n"
            "        assert True\n",
            "collide with module fixtures",
            id="module-class-collision",
        ),
    ],
)
def test_docs_governance_fails_closed_on_ambiguous_fixture_names(source: str, message: str) -> None:
    """Unsafe fixture-name declarations cannot silently remove a reader edge."""

    with pytest.raises(AssertionError, match=message):
        _docs_reading_test_modules(source)


@pytest.mark.docs_ci
def test_docs_governance_resolves_class_scoped_fixture_dependencies() -> None:
    """Class tests resolve class and module fixtures without scope leakage."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def module_plain() -> int:\n"
        "    return 1\n\n"
        "def test_module_does_not_see_class_fixture(class_docs: str) -> None:\n"
        "    assert class_docs\n\n"
        "class TestScoped:\n"
        "    @pytest.fixture(name='class_docs')\n"
        "    def docs_leaf(self) -> str:\n"
        '        return Path("docs/class-fixture.md").read_text()\n\n'
        "    @pytest.fixture\n"
        "    def docs_middle(self, class_docs: str) -> str:\n"
        "        return class_docs\n\n"
        "    @pytest.fixture\n"
        "    def plain_fixture(self, module_plain: int) -> int:\n"
        "        return module_plain\n\n"
        "    def test_direct(self, class_docs: str) -> None:\n"
        "        assert class_docs\n\n"
        "    def test_transitive(self, docs_middle: str) -> None:\n"
        "        assert docs_middle\n\n"
        "    def test_plain(self, plain_fixture: int) -> None:\n"
        "        assert plain_fixture == 1\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestScoped::test_direct",
        "TestScoped::test_transitive",
    }


@pytest.mark.docs_ci
def test_docs_governance_follows_transitive_fixture_parameters() -> None:
    """Fixture dependency parameters contribute bounded transitive read edges."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def docs_leaf() -> str:\n"
        '    return Path("docs/fixture.md").read_text()\n\n'
        "@pytest.fixture\n"
        "def docs_middle(docs_leaf: str) -> str:\n"
        "    return docs_leaf\n\n"
        "def test_docs_fixture(docs_middle: str) -> None:\n"
        "    assert docs_middle\n\n"
        "@pytest.fixture\n"
        "async def async_leaf() -> str:\n"
        '    return Path("docs/async-fixture.md").read_text()\n\n'
        "@pytest.fixture\n"
        "async def async_middle(async_leaf: str) -> str:\n"
        "    return async_leaf\n\n"
        "async def test_async_fixture(async_middle: str) -> None:\n"
        "    assert async_middle\n\n"
        "@pytest.fixture\n"
        "def plain_leaf() -> int:\n"
        "    return 1\n\n"
        "@pytest.fixture\n"
        "def plain_middle(plain_leaf: int) -> int:\n"
        "    return plain_leaf\n\n"
        "def test_plain_fixture(plain_middle: int) -> None:\n"
        "    assert plain_middle == 1\n\n"
        "@pytest.fixture\n"
        "def cycle_left(cycle_right: str) -> str:\n"
        "    return cycle_right\n\n"
        "@pytest.fixture\n"
        "def cycle_right(cycle_left: str) -> str:\n"
        "    return cycle_left\n\n"
        "def test_cycle(cycle_left: str) -> None:\n"
        "    assert cycle_left\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_async_fixture",
        "test_docs_fixture",
    }


@pytest.mark.docs_ci
def test_docs_governance_follows_invoked_local_def_and_ignores_unused_one() -> None:
    """An invoked nested def propagates its read; an unused nested def does not."""

    source = (
        "from pathlib import Path\n\n"
        "def test_local() -> None:\n"
        "    def load() -> str:\n"
        '        return Path("docs/local.md").read_text()\n\n'
        "    def unused() -> str:\n"
        '        return Path("docs/unused.md").read_text()\n\n'
        "    assert load()\n\n"
        "def test_non_reader() -> None:\n"
        "    def unused() -> str:\n"
        '        return Path("docs/never.md").read_text()\n\n'
        "    assert True\n"
    )
    assert _docs_reading_test_modules(source) == {"test_local"}


@pytest.mark.docs_ci
def test_docs_governance_follows_transitive_async_and_lambda_local_helpers() -> None:
    """Local def, async def, and lambda edges propagate through the bounded graph."""

    source = (
        "from pathlib import Path\n\n"
        "async def test_async_local() -> None:\n"
        "    async def middle() -> str:\n"
        "        return await leaf()\n\n"
        "    async def leaf() -> str:\n"
        '        return Path("docs/async.md").read_text()\n\n'
        "    assert await middle()\n\n"
        "def test_lambda_local() -> None:\n"
        '    load = lambda: Path("docs/lambda.md").read_text()\n'
        "    assert load()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_async_local", "test_lambda_local"}


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_an_ambiguous_invoked_local_helper() -> None:
    """An invoked locally rebound helper name is a named fail-closed edge."""

    source = (
        "def test_ambiguous() -> None:\n"
        "    def load() -> str:\n"
        "        return ''\n\n"
        "    def load() -> str:\n"
        "        return ''\n\n"
        "    assert load()\n"
    )
    with pytest.raises(AssertionError, match="unresolved local helper edge"):
        _docs_reading_test_modules(source, filename="local_ambiguous.py")


@pytest.mark.docs_ci
def test_docs_governance_detects_direct_class_readers_without_siblings() -> None:
    """Class test methods use qualified prefixes and do not select non-readers."""

    source = (
        "from pathlib import Path\n\n"
        "class TestDocs:\n"
        "    def test_reader(self) -> None:\n"
        '        Path("docs/class.md").read_text()\n\n'
        "    def test_sibling(self) -> None:\n"
        "        assert True\n"
    )
    assert _docs_reading_test_modules(source) == {"TestDocs::test_reader"}


@pytest.mark.docs_ci
def test_docs_governance_rejects_module_scope_docs_reads_with_collected_tests() -> None:
    """Import-time docs content cannot be hidden behind marked or unmarked tests."""
    no_test = 'from pathlib import Path\ncached = Path("docs/cache.md").read_text()\n'
    assert _docs_reading_test_modules(no_test) == set()

    for source in (
        no_test + "\ndef test_unmarked() -> None:\n    assert cached\n",
        "from pathlib import Path\nimport pytest\n"
        'cached = Path("docs/cache.md").read_text()\n\n'
        "@pytest.mark.docs\ndef test_marked() -> None:\n    assert cached\n",
    ):
        with pytest.raises(AssertionError, match="module-scope docs read cannot be attributed"):
            _docs_reading_test_modules(source, filename="module_cache.py")


@pytest.mark.docs_ci
def test_docs_governance_rejects_ambiguous_module_scope_docs_reads() -> None:
    """A dynamic module docs filename cannot evade the import-time read boundary."""
    source = (
        "from pathlib import Path\n\n"
        "dynamic_name = get_dynamic_name()\n"
        'cached = Path("docs", dynamic_name).read_text()\n\n'
        "def test_collected() -> None:\n    assert cached\n"
    )

    with pytest.raises(AssertionError, match="module-scope docs read receiver is ambiguous"):
        _docs_reading_test_modules(source, filename="module_dynamic_cache.py")


@pytest.mark.docs_ci
def test_docs_governance_rejects_unaudited_inherited_collected_test_methods() -> None:
    """A collected subclass cannot silently inherit a local docs reader."""
    standalone = "class TestStandalone:\n    def test_plain(self) -> None:\n        assert True\n"
    assert _docs_reading_test_modules(standalone) == set()

    for source in (
        "from pathlib import Path\n\nclass LocalBase:\n"
        "    def test_reader(self) -> None:\n        Path('docs/base.md').read_text()\n\n"
        "class TestChild(LocalBase):\n    pass\n",
        "from pathlib import Path\nimport pytest\n\nclass LocalBase:\n"
        "    def test_reader(self) -> None:\n        Path('docs/base.md').read_text()\n\n"
        "@pytest.mark.docs\nclass TestChild(LocalBase):\n    pass\n",
    ):
        with pytest.raises(AssertionError, match="unaudited inherited base"):
            _docs_reading_test_modules(source, filename="inherited_test.py")


@pytest.mark.docs_ci
def test_docs_governance_class_marker_must_not_overmark_siblings() -> None:
    """A class marker is valid only when every marked method is a docs reader."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.docs\n"
        "class TestDocs:\n"
        "    def test_reader(self) -> None:\n"
        '        Path("docs/class.md").read_text()\n\n'
        "    def test_sibling(self) -> None:\n"
        "        assert True\n"
    )
    tree = ast.parse(source)
    readers = _docs_reading_test_modules(source)
    assert _marked_docs_test_prefixes(tree) == {
        "TestDocs::test_reader",
        "TestDocs::test_sibling",
    }
    with pytest.raises(AssertionError, match="must sit on exactly the readers"):
        _assert_exact_docs_markers(tree, readers, "class_overmark.py")


@pytest.mark.docs_ci
def test_docs_governance_accepts_exact_class_and_method_markers() -> None:
    """Class markers cover all-reader classes; method markers cover only that method."""

    class_source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.docs\n"
        "class TestAllReaders:\n"
        "    def test_one(self) -> None:\n"
        '        Path("docs/one.md").read_text()\n\n'
        "    def test_two(self) -> None:\n"
        '        Path("docs/two.md").read_text()\n'
    )
    method_source = (
        "from pathlib import Path\nimport pytest\n\n"
        "class TestMethodMarker:\n"
        "    @pytest.mark.docs\n"
        "    def test_reader(self) -> None:\n"
        '        Path("docs/method.md").read_text()\n\n'
        "    def test_sibling(self) -> None:\n"
        "        assert True\n"
    )
    for source in (class_source, method_source):
        tree = ast.parse(source)
        _assert_exact_docs_markers(tree, _docs_reading_test_modules(source), "marked_class.py")


@pytest.mark.docs_ci
def test_docs_governance_follows_class_methods_to_top_level_and_same_class_helpers() -> None:
    """Class readers reach both top-level fixtures and bounded same-class helpers."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def runbook() -> str:\n"
        '    return Path("docs/fixture.md").read_text()\n\n'
        "def load_top_level() -> str:\n"
        '    return Path("docs/helper.md").read_text()\n\n'
        "class TestDocs:\n"
        "    def _load_self(self) -> str:\n"
        '        return Path("docs/self.md").read_text()\n\n'
        "    def test_fixture(self, runbook: str) -> None:\n"
        "        assert runbook\n\n"
        "    def test_top_level_helper(self) -> None:\n"
        "        assert load_top_level()\n\n"
        "    def test_same_class_helper(self) -> None:\n"
        "        assert self._load_self()\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestDocs::test_fixture",
        "TestDocs::test_top_level_helper",
        "TestDocs::test_same_class_helper",
    }


@pytest.mark.docs_ci
def test_docs_governance_follows_class_qualified_helpers() -> None:
    """Static class-qualified calls share the same bounded helper graph as self calls."""

    source = (
        "from pathlib import Path\n\n"
        "class TestDocs:\n"
        "    @staticmethod\n"
        "    def leaf() -> str:\n"
        '        return Path("docs/class-qualified.md").read_text()\n\n'
        "    @classmethod\n"
        "    def middle(cls) -> str:\n"
        "        return TestDocs.leaf()\n\n"
        "    @staticmethod\n"
        "    def path() -> Path:\n"
        '        return Path("docs/class-return.md")\n\n'
        "    @staticmethod\n"
        "    def non_docs_path() -> Path:\n"
        '        return Path("config/class-return.md")\n\n'
        "    def test_reader(self) -> None:\n"
        "        assert TestDocs.middle()\n\n"
        "    def test_returned_path(self) -> None:\n"
        "        TestDocs.path().read_text()\n\n"
        "    def test_non_docs_path(self) -> None:\n"
        "        TestDocs.non_docs_path().read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestDocs::test_reader",
        "TestDocs::test_returned_path",
    }


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_an_unresolved_same_class_call() -> None:
    """An unresolvable self/cls edge cannot silently hide a docs reader."""

    source = (
        "class TestDocs:\n    def test_dynamic(self) -> None:\n        self._dynamic_reader()\n"
    )
    with pytest.raises(AssertionError, match="unresolved same-class call edge"):
        _docs_reading_test_modules(source, filename="class_dynamic.py")


@pytest.mark.docs_ci
def test_docs_governance_rejects_missing_or_surplus_class_method_markers() -> None:
    """Removing a class-reader marker or adding one to a sibling reds exactness."""

    missing = (
        "from pathlib import Path\n\n"
        "class TestMissing:\n"
        "    def test_reader(self) -> None:\n"
        '        Path("docs/missing.md").read_text()\n'
    )
    surplus = (
        "import pytest\n\n"
        "class TestSurplus:\n"
        "    @pytest.mark.docs\n"
        "    def test_non_reader(self) -> None:\n"
        "        assert True\n"
    )
    for source in (missing, surplus):
        tree = ast.parse(source)
        with pytest.raises(AssertionError, match="must sit on exactly the readers"):
            _assert_exact_docs_markers(tree, _docs_reading_test_modules(source), "class_marker.py")


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_an_unresolved_dynamic_call_edge() -> None:
    """A partial-evidence receiver raises a named reason rather than silently passing.

    ``get_dynamic_receiver("docs")`` folds a literal ``docs`` component but no
    provable ``.md``-suffixed segment — the closed rule cannot prove the
    later read either does or does not reach committed docs content, so it
    fails the governance test with a named file:line reason instead of
    silently under-marking (a false negative) or over-marking every dynamic
    receiver in the suite (a flood of false positives).
    """

    source = (
        "def test_dynamic() -> None:\n"
        '    receiver = get_dynamic_receiver("docs")\n'
        "    receiver.read_text()\n"
    )
    with pytest.raises(AssertionError, match="unresolved dynamic read receiver"):
        _docs_reading_test_modules(source, filename="dynamic_example.py")


@pytest.mark.docs_ci
def test_docs_governance_traces_repository_imported_fixture_dependencies(tmp_path: Path) -> None:
    """Imported first-party fixtures reach signature, mark, and request consumers only."""

    (tmp_path / "fixtures.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\ndef docs_fixture() -> str:\n"
        "    return Path('docs/imported-fixture.md').read_text()\n\n"
        "@pytest.fixture\ndef config_fixture() -> str:\n"
        "    return Path('config/imported-fixture.md').read_text()\n"
    )
    source = (
        "from fixtures import config_fixture, docs_fixture\nimport pytest\n\n"
        "def test_signature(docs_fixture: str) -> None:\n    assert docs_fixture\n\n"
        "@pytest.mark.usefixtures('docs_fixture')\ndef test_mark() -> None:\n    assert True\n\n"
        "def test_request(request) -> None:\n"
        "    assert request.getfixturevalue('docs_fixture')\n\n"
        "def test_non_reader(config_fixture: str) -> None:\n    assert config_fixture\n"
    )
    assert _docs_reading_test_modules(source, repository_root=tmp_path) == {
        "test_mark",
        "test_request",
        "test_signature",
    }

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "fixtures.py").write_text((tmp_path / "fixtures.py").read_text())
    with pytest.raises(AssertionError, match="imported fixture provenance is ambiguous"):
        _docs_reading_test_modules(
            "from fixtures import docs_fixture\n\n"
            "def test_signature(docs_fixture):\n    assert docs_fixture\n",
            filename=str(tests_root / "test_import.py"),
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_traces_source_root_imports_and_imported_autouse_fixtures(
    tmp_path: Path,
) -> None:
    """First-party ``src`` helpers and plugin autouse fixtures reach consumers."""

    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "helpers.py").write_text(
        "from pathlib import Path\n\ndef docs_helper():\n"
        "    return Path('docs/src-helper.md').read_text()\n\n"
        "def config_helper():\n"
        "    return Path('config/src-helper.md').read_text()\n"
    )
    (source_root / "plugin.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture(autouse=True)\ndef docs_auto():\n"
        "    return Path('docs/src-autouse.md').read_text()\n\n"
        "@pytest.fixture\ndef config_fixture():\n"
        "    return Path('config/src-fixture.md').read_text()\n"
    )
    (source_root / "manual_plugin.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\ndef docs_manual():\n"
        "    return Path('docs/src-manual.md').read_text()\n"
    )
    helper_source = (
        "from helpers import config_helper, docs_helper\n\n"
        "def test_docs() -> None:\n    assert docs_helper()\n\n"
        "def test_non_reader() -> None:\n    assert config_helper()\n"
    )
    assert _docs_reading_test_modules(helper_source, repository_root=tmp_path) == {"test_docs"}
    (tmp_path / "helpers.py").write_text("def docs_helper():\n    return 'conflict'\n")
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(helper_source, repository_root=tmp_path)
    (tmp_path / "helpers.py").unlink()
    with pytest.raises(AssertionError, match="imported callable provenance"):
        _docs_reading_test_modules(
            "from .missing import docs_helper\n\ndef test_missing() -> None:\n    docs_helper()\n",
            filename="tests/test_missing.py",
            repository_root=tmp_path,
        )
    plugin_source = "pytest_plugins = 'plugin'\n\ndef test_autouse() -> None:\n    assert True\n"
    assert _docs_reading_test_modules(plugin_source, repository_root=tmp_path) == {"test_autouse"}
    direct_import_source = (
        "from plugin import docs_auto\n\ndef test_direct_autouse() -> None:\n    assert True\n"
    )
    assert _docs_reading_test_modules(direct_import_source, repository_root=tmp_path) == {
        "test_direct_autouse"
    }
    assert (
        _docs_reading_test_modules(
            "pytest_plugins = 'manual_plugin'\n\n"
            "def test_non_autouse() -> None:\n    assert True\n",
            repository_root=tmp_path,
        )
        == set()
    )
    with pytest.raises(AssertionError, match="imported fixture provenance is ambiguous"):
        _docs_reading_test_modules(
            "from .missing import docs_auto\n\n"
            "def test_missing_fixture(docs_auto) -> None:\n    assert docs_auto\n",
            filename="tests/test_missing_fixture.py",
            repository_root=tmp_path,
        )


@pytest.mark.docs_ci
def test_docs_governance_bounds_source_root_production_package_exclusion(tmp_path: Path) -> None:
    """Only the exact production package root stays outside static ``src`` resolution."""

    source_root = tmp_path / "src"
    source_root.mkdir()
    production_package = source_root / "roastpilot_agent.py"
    similarly_prefixed_module = source_root / "roastpilot_agent_tools.py"
    production_package.write_text("VALUE = 'production'\n")
    similarly_prefixed_module.write_text("VALUE = 'helper'\n")

    assert (
        _repository_module_candidates("roastpilot_agent", "tests/test_example.py", tmp_path)
        == set()
    )
    assert _repository_module_candidates(
        "roastpilot_agent_tools", "tests/test_example.py", tmp_path
    ) == {similarly_prefixed_module}


@pytest.mark.docs_ci
def test_docs_governance_traces_import_time_decorators_literal_loops_and_finalizers() -> None:
    """Only executed decorators, bounded loops, and finalizers add reader provenance."""

    source = (
        "from pathlib import Path\n\n"
        "@Path('docs/function-decorator.md').read_text()\n"
        "def test_function_decorator() -> None:\n    assert True\n\n"
        "@Path('docs/async-decorator.md').read_text()\n"
        "async def test_async_decorator() -> None:\n    assert True\n\n"
        "@Path('docs/class-decorator.md').read_text()\n"
        "class TestDecorators:\n"
        "    @Path('docs/method-decorator.md').read_text()\n"
        "    def test_method(self) -> None:\n        assert True\n\n"
        "@Path('docs/class-only-decorator.md').read_text()\n"
        "class TestClassDecoratorOnly:\n"
        "    def test_method(self) -> None:\n        assert True\n\n"
        "def test_literal_loop() -> None:\n"
        "    for path in ['docs/loop-a.md', 'docs/loop-b.md']:\n"
        "        path.read_text()\n\n"
        "def test_finalizer(request) -> None:\n"
        "    request.addfinalizer(lambda: Path('docs/finalizer.md').read_text())\n\n"
        "def test_finalizer_non_reader(request) -> None:\n"
        "    request.addfinalizer(lambda: Path('config/finalizer.md').read_text())\n\n"
        "@Path('config/non-reader.md').read_text()\n"
        "def test_non_reader() -> None:\n"
        "    for path in ['config/loop.md']:\n"
        "        path.read_text()\n"
        "    callback = lambda: Path('docs/inert.md').read_text()\n"
        "    assert callback\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestClassDecoratorOnly::test_method",
        "TestDecorators::test_method",
        "test_async_decorator",
        "test_finalizer",
        "test_function_decorator",
        "test_literal_loop",
    }
    for ambiguous in (
        source.replace("'docs/loop-b.md'", "'config/loop.md'"),
        source.replace("'docs/finalizer.md'", "'docs', dynamic_name"),
        source.replace("'docs/function-decorator.md'", "'docs', dynamic_name"),
    ):
        with pytest.raises(AssertionError, match="ambiguous|cannot prove"):
            _docs_reading_test_modules(ambiguous)
    with pytest.raises(AssertionError, match="addfinalizer callback provenance is ambiguous"):
        _docs_reading_test_modules(
            "from pathlib import Path\n\n"
            "def named_callback() -> None:\n"
            "    Path('docs/named-finalizer.md').read_text()\n\n"
            "def test_named_finalizer(request) -> None:\n"
            "    request.addfinalizer(named_callback)\n"
        )


@pytest.mark.docs_ci
def test_docs_governance_traces_literal_comprehension_targets() -> None:
    """Literal comprehension iterables preserve docs provenance for their targets."""

    source = (
        "from pathlib import Path\n\n"
        "def test_list() -> None:\n"
        "    assert [path.read_text() for path in ['docs/list-a.md', 'docs/list-b.md']]\n\n"
        "def test_tuple() -> None:\n"
        "    assert [path.read_text() for path in ('docs/tuple.md',)]\n\n"
        "def test_set() -> None:\n"
        "    assert [path.read_text() for path in {'docs/set.md'}]\n\n"
        "def test_non_reader() -> None:\n"
        "    assert [path.read_text() for path in ['config/non-reader.md']]\n"
    )
    assert _docs_reading_test_modules(source) == {"test_list", "test_set", "test_tuple"}
    for ambiguous in (
        source.replace("'docs/list-b.md'", "'config/list.md'"),
        source.replace("'docs/list-b.md'", "dynamic_path"),
    ):
        with pytest.raises(AssertionError, match="cannot prove|unresolved"):
            _docs_reading_test_modules(ambiguous)


@pytest.mark.docs_ci
def test_docs_governance_fails_closed_on_imported_collected_test_class_aliases(
    tmp_path: Path,
) -> None:
    """Relevant imported classes cannot be re-exposed as unanalysed pytest classes."""

    (tmp_path / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "class DocsCase:\n"
        "    def test_reader(self) -> None:\n"
        "        Path('docs/imported-test-class.md').read_text()\n\n"
        "class ConfigCase:\n"
        "    def test_reader(self) -> None:\n"
        "        assert True\n"
    )
    with pytest.raises(AssertionError, match="imported test-class alias"):
        _docs_reading_test_modules(
            "from helpers import ConfigCase, DocsCase\n\n"
            "TestDocs = DocsCase\n"
            "TestConfig = ConfigCase\n",
            repository_root=tmp_path,
        )
    assert (
        _docs_reading_test_modules(
            "from helpers import ConfigCase, DocsCase\n\n"
            "TestConfig = ConfigCase\n"
            "DocsAlias = DocsCase\n",
            repository_root=tmp_path,
        )
        == set()
    )


@pytest.mark.docs_ci
def test_docs_governance_tracks_monkeypatch_working_directory_provenance() -> None:
    """A local static ``chdir`` cannot hide a subsequent relative Markdown read."""

    source = (
        "from pathlib import Path\n\n"
        "def test_docs(monkeypatch) -> None:\n"
        "    monkeypatch.chdir('docs')\n"
        "    Path('guide.md').read_text()\n\n"
        "def test_restored(monkeypatch) -> None:\n"
        "    monkeypatch.chdir('docs')\n"
        "    monkeypatch.chdir('..')\n"
        "    Path('guide.md').read_text()\n\n"
        "def test_config(monkeypatch) -> None:\n"
        "    monkeypatch.chdir('config')\n"
        "    Path('guide.md').read_text()\n\n"
        "def test_other_scope() -> None:\n"
        "    Path('guide.md').read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {"test_docs"}
    with pytest.raises(AssertionError, match="working-directory provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace("monkeypatch.chdir('docs')", "monkeypatch.chdir(dynamic_directory)", 1)
        )


@pytest.mark.docs_ci
def test_docs_governance_traces_static_pytest_generate_tests_parameters() -> None:
    """Static generated docs parameters seed only their matching read receivers."""

    source = (
        "def pytest_generate_tests(metafunc) -> None:\n"
        "    if metafunc.function.__name__ == 'test_generated':\n"
        "        metafunc.parametrize('path', ['docs/generated.md'])\n\n"
        "def test_generated(path) -> None:\n    path.read_text()\n\n"
        "def test_unrelated(value) -> None:\n    assert value\n"
    )
    assert _docs_reading_test_modules(source) == {"test_generated"}
    dynamic = source.replace("['docs/generated.md']", "build_paths()")
    with pytest.raises(AssertionError, match="parametrized receiver `path` is ambiguous"):
        _docs_reading_test_modules(dynamic)


@pytest.mark.docs_ci
def test_docs_governance_traces_static_class_attributes() -> None:
    """Static docs class attributes propagate through self, cls, and class names."""

    source = (
        "from pathlib import Path\n\n"
        "class TestAttributes:\n"
        "    DOCS_PATH = Path('docs/class-attribute.md')\n"
        "    CONFIG_PATH = Path('config/class-attribute.md')\n\n"
        "    @classmethod\n    def _path(cls):\n        return cls.DOCS_PATH\n\n"
        "    def test_self(self) -> None:\n        self.DOCS_PATH.read_text()\n\n"
        "    def test_cls(self) -> None:\n        self._path().read_text()\n\n"
        "    def test_qualified(self) -> None:\n        TestAttributes.DOCS_PATH.read_text()\n\n"
        "    def test_non_reader(self) -> None:\n        self.CONFIG_PATH.read_text()\n"
    )
    assert _docs_reading_test_modules(source) == {
        "TestAttributes::test_cls",
        "TestAttributes::test_qualified",
        "TestAttributes::test_self",
    }
    ambiguous = source.replace("Path('docs/class-attribute.md')", "Path('docs') / dynamic_name")
    with pytest.raises(AssertionError, match="cannot prove this is or is not a docs read"):
        _docs_reading_test_modules(ambiguous)


@pytest.mark.docs_ci
def test_docs_governance_traces_same_module_helper_class_dispatch() -> None:
    """Constructor and class-style helper calls add bounded same-module reader edges."""

    source = (
        "from pathlib import Path\n\n"
        "class DocsLoader:\n"
        "    def load(self) -> str:\n        return Path('docs/helper-class.md').read_text()\n\n"
        "    @staticmethod\n    def static() -> str:\n"
        "        return Path('docs/helper-class-static.md').read_text()\n\n"
        "class ConfigLoader:\n"
        "    def load(self) -> str:\n        return Path('config/helper-class.md').read_text()\n\n"
        "def test_chained() -> None:\n    assert DocsLoader().load()\n\n"
        "def test_assigned() -> None:\n"
        "    loader = DocsLoader()\n    alias = loader\n    assert alias.load()\n\n"
        "def test_static() -> None:\n    assert DocsLoader.static()\n\n"
        "def test_non_reader() -> None:\n    assert ConfigLoader().load()\n"
    )
    assert _docs_reading_test_modules(source) == {
        "test_assigned",
        "test_chained",
        "test_static",
    }
    with pytest.raises(AssertionError, match="helper-class call edge"):
        _docs_reading_test_modules(source.replace("DocsLoader.static()", "DocsLoader.dynamic()"))
    with pytest.raises(AssertionError, match="helper-class instance provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace("loader = DocsLoader()", "loader = construct(DocsLoader)")
        )


@pytest.mark.docs_ci
def test_docs_governance_boundary_mutations_red_against_exact_markers(tmp_path: Path) -> None:
    """Each of the six admitted provenance boundaries has a direct red mutation proof."""

    cases = [
        (
            "import pytest\nimport pytest as pt\nfrom pathlib import Path\n\n"
            "@pt.fixture\ndef value():\n"
            "    return Path('docs/alias.md').read_text()\n\n@pytest.mark.docs\n"
            "def test_alias(value):\n    assert value\n",
            "alias",
        ),
        (
            "import pytest\nfrom pathlib import Path\n\ndef setup_function():\n"
            "    Path('docs/hook.md').read_text()\n\n@pytest.mark.docs\n"
            "def test_hook():\n    assert True\n",
            "xunit",
        ),
        (
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('path', ['docs/generated.md'])\n\n"
            "import pytest\n@pytest.mark.docs\ndef test_generated(path):\n    path.read_text()\n",
            "generated",
        ),
        (
            "from pathlib import Path\nimport pytest\n\nclass TestAttrs:\n"
            "    PATH = Path('docs/attribute.md')\n\n    @pytest.mark.docs\n"
            "    def test_attribute(self):\n        self.PATH.read_text()\n",
            "attribute",
        ),
        (
            "from pathlib import Path\nimport pytest\n\nclass Loader:\n"
            "    def load(self):\n        return Path('docs/helper.md').read_text()\n\n"
            "@pytest.mark.docs\ndef test_helper():\n    assert Loader().load()\n",
            "helper",
        ),
    ]
    (tmp_path / "fixtures.py").write_text(
        "from pathlib import Path\nimport pytest\n\n@pytest.fixture\ndef imported():\n"
        "    return Path('docs/imported.md').read_text()\n"
    )
    imported = (
        "from fixtures import imported\nimport pytest\n\n@pytest.mark.docs\n"
        "def test_imported(imported):\n    assert imported\n"
    )
    for source, label in cases:
        readers = _docs_reading_test_modules(source)
        _assert_exact_docs_markers(ast.parse(source), readers, f"{label}.py")
        mutated = source.replace("docs/", "config/", 1)
        with pytest.raises(AssertionError, match="must sit on exactly the readers"):
            _assert_exact_docs_markers(
                ast.parse(mutated), _docs_reading_test_modules(mutated), f"{label}-mutated.py"
            )
    readers = _docs_reading_test_modules(imported, repository_root=tmp_path)
    _assert_exact_docs_markers(ast.parse(imported), readers, "imported.py")
    mutated_imported = (tmp_path / "fixtures.py").read_text().replace("docs/", "config/", 1)
    (tmp_path / "fixtures.py").write_text(mutated_imported)
    with pytest.raises(AssertionError, match="must sit on exactly the readers"):
        _assert_exact_docs_markers(
            ast.parse(imported),
            _docs_reading_test_modules(imported, repository_root=tmp_path),
            "imported.py",
        )


@pytest.mark.docs_ci
@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param("pytest_plugins = 'plugin'", id="string"),
        pytest.param("pytest_plugins = ['plugin']", id="list"),
        pytest.param(
            "plugin_modules = ('plugin',)\npytest_plugins = plugin_modules", id="tuple-name"
        ),
    ],
)
def test_docs_governance_traces_literal_pytest_plugin_fixtures(
    tmp_path: Path, declaration: str
) -> None:
    """Literal local plugin declarations expose only their docs fixture consumers."""

    (tmp_path / "plugin.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture\ndef docs_fixture() -> str:\n"
        "    return Path('docs/plugin.md').read_text()\n\n"
        "@pytest.fixture\ndef config_fixture() -> str:\n"
        "    return Path('config/plugin.md').read_text()\n"
    )
    source = (
        f"{declaration}\n\n"
        "def test_docs(docs_fixture: str) -> None:\n    assert docs_fixture\n\n"
        "def test_non_reader(config_fixture: str) -> None:\n    assert config_fixture\n"
    )
    assert _docs_reading_test_modules(source, repository_root=tmp_path) == {"test_docs"}
    for invalid in (
        "pytest_plugins = build_plugins()",
        "pytest_plugins = 'plugin'\npytest_plugins = ('plugin',)",
        "pytest_plugins = 'missing_plugin'",
    ):
        with pytest.raises(AssertionError, match="pytest_plugins"):
            _docs_reading_test_modules(
                invalid + "\n\ndef test_docs(docs_fixture):\n    assert docs_fixture\n",
                repository_root=tmp_path,
            )


@pytest.mark.docs_ci
def test_docs_governance_traces_resolved_test_decorators_only() -> None:
    """Local wrapper and factory decorators add docs edges without marking pytest metadata."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "def docs_wrapper(func):\n    Path('docs/wrapper.md').read_text()\n    return func\n\n"
        "def docs_factory():\n    Path('docs/factory.md').read_text()\n"
        "    return lambda func: func\n\n"
        "def config_wrapper(func):\n    Path('config/wrapper.md').read_text()\n    return func\n\n"
        "@docs_wrapper\ndef test_wrapped() -> None:\n    assert True\n\n"
        "@docs_factory()\ndef test_factory() -> None:\n    assert True\n\n"
        "@config_wrapper\ndef test_non_reader() -> None:\n    assert True\n\n"
        "@pytest.mark.parametrize('value', [1])\ndef test_pytest_marker(value: int) -> None:\n"
        "    assert value\n"
    )
    assert _docs_reading_test_modules(source) == {"test_factory", "test_wrapped"}
    with pytest.raises(AssertionError, match="decorator provenance"):
        _docs_reading_test_modules(
            source.replace("@docs_wrapper", "docs_wrapper = object()\n\n@docs_wrapper")
        )


@pytest.mark.docs_ci
def test_docs_governance_resolves_bare_decorator_aliases_fail_closed() -> None:
    """Bare local decorator aliases retain only statically unique provenance."""

    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "def docs_wrapper(func):\n    Path('docs/wrapper.md').read_text()\n    return func\n\n"
        "def docs_factory():\n    Path('docs/factory.md').read_text()\n"
        "    return lambda func: func\n\n"
        "def config_wrapper(func):\n    Path('config/wrapper.md').read_text()\n    return func\n\n"
        "docs_alias = docs_wrapper\ndocs_chain = docs_alias\n"
        "factory_alias = docs_factory\nconfig_alias = config_wrapper\n"
        "mark = pytest.mark\nparametrize = pytest.mark.parametrize\n\n"
        "@docs_chain\ndef test_wrapped() -> None:\n    assert True\n\n"
        "@factory_alias()\ndef test_factory() -> None:\n    assert True\n\n"
        "@config_alias\ndef test_non_reader() -> None:\n    assert True\n\n"
        "@parametrize('value', [1])\ndef test_marker_alias(value: int) -> None:\n"
        "    assert value\n\n"
        "@mark.parametrize('value', [1])\ndef test_marker_module_alias(value: int) -> None:\n"
        "    assert value\n"
    )
    assert _docs_reading_test_modules(source) == {"test_factory", "test_wrapped"}
    for ambiguous in (
        source.replace(
            "docs_chain = docs_alias", "docs_chain = docs_alias\ndocs_chain = config_wrapper"
        ),
        source.replace("docs_chain = docs_alias", "docs_chain = build_wrapper()"),
        source.replace(
            "docs_alias = docs_wrapper",
            "docs_wrapper = other = dynamic_value\ndocs_alias = docs_wrapper",
        ),
        source.replace("docs_alias = docs_wrapper", "docs_alias = second_alias = docs_wrapper"),
    ):
        with pytest.raises(AssertionError, match="decorator provenance"):
            _docs_reading_test_modules(ambiguous)

    marked = source.replace("@docs_chain", "@pytest.mark.docs\n@docs_chain").replace(
        "@factory_alias()", "@pytest.mark.docs\n@factory_alias()"
    )
    tree = ast.parse(marked)
    readers = _docs_reading_test_modules(marked)
    _assert_exact_docs_markers(tree, readers, "decorator-alias.py")
    mutated = marked.replace("docs_chain = docs_alias", "docs_chain = config_wrapper")
    with pytest.raises(AssertionError, match="must sit on exactly the readers"):
        _assert_exact_docs_markers(
            ast.parse(mutated), _docs_reading_test_modules(mutated), "decorator-alias-mutated.py"
        )


@pytest.mark.docs_ci
def test_docs_governance_traces_literal_subprocess_docs_paths() -> None:
    """Admitted subprocess aliases retain literal docs Markdown argument provenance."""

    source = (
        "import subprocess as sp\nfrom subprocess import check_output as output\n\n"
        "def test_qualified() -> None:\n    sp.run(['cat', 'docs/qualified.md'])\n\n"
        "def test_alias() -> None:\n    output(('cat', 'docs/alias.md'))\n\n"
        "def test_non_reader() -> None:\n    sp.run(['cat', 'config/value.md'])\n"
    )
    assert _docs_reading_test_modules(source) == {"test_alias", "test_qualified"}
    for ambiguous in (
        source.replace("'docs/qualified.md'", "'docs' / dynamic_name"),
        source.replace("import subprocess as sp", "import subprocess as sp\nsp = object()"),
        source.replace("check_output as output", "check_output as output\noutput = object()"),
    ):
        with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
            _docs_reading_test_modules(ambiguous)


@pytest.mark.docs_ci
def test_docs_governance_tracks_static_subprocess_command_aliases() -> None:
    """Assigned static subprocess command containers retain closed path provenance."""

    source = (
        "import subprocess as sp\nfrom subprocess import run as execute\n\n"
        "def test_docs() -> None:\n"
        "    runner = sp.run\n"
        "    command = ['cat', 'docs/assigned.md']\n"
        "    runner(command)\n\n"
        "def test_direct_alias_docs() -> None:\n"
        "    runner = execute\n"
        "    command = ['cat', 'docs/direct-assigned.md']\n"
        "    runner(command)\n\n"
        "def test_non_docs() -> None:\n"
        "    command = ['cat', 'config/assigned.md']\n"
        "    sp.run(command)\n"
    )
    assert _docs_reading_test_modules(source) == {"test_direct_alias_docs", "test_docs"}
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("'docs/assigned.md'", "'docs', dynamic_name"))
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("runner(command)", "runner(*command)"))
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace("runner(command)", "runner(*['cat', 'docs/starred.md'])")
        )
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("runner(command)", "runner(command, **options)"))
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace("runner(command)", "runner = object()\n    runner(command)")
        )
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace("runner(command)", "command = dynamic_command\n    runner(command)")
        )
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("runner(command)", "runner(build_command())"))
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(
            source.replace(
                "def test_non_docs() -> None:\n    command = ['cat', 'config/assigned.md']",
                "def test_non_docs() -> None:\n    command = build_command()",
            )
        )
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(source.replace("'config/assigned.md'", "dynamic_name"))
    conditional_source = (
        "import subprocess\n\n"
        "def test_branch(condition: bool) -> None:\n"
        "    command = ['cat', 'docs/branch.md']\n"
        "    if condition:\n"
        "        command = ['cat', 'config/branch.md']\n"
        "    subprocess.run(command)\n\n"
        "def test_branch_call_order(condition: bool) -> None:\n"
        "    command = ['cat', 'docs/branch-order.md']\n"
        "    if condition:\n"
        "        command = ['cat', 'config/branch-order.md']\n"
        "        subprocess.run(command)\n"
        "    else:\n"
        "        subprocess.run(command)\n"
    )
    with pytest.raises(AssertionError, match="subprocess docs-path provenance is ambiguous"):
        _docs_reading_test_modules(conditional_source)


@pytest.mark.docs_ci
def test_docs_governance_new_boundary_mutations_red_against_exact_markers(
    tmp_path: Path,
) -> None:
    """Plugin, decorator, and subprocess provenance mutations each red exact markers."""

    cases = [
        (
            "from pathlib import Path\nimport pytest\n\n"
            "def wrapper(func):\n    Path('docs/decorator.md').read_text()\n    return func\n\n"
            "@wrapper\n@pytest.mark.docs\ndef test_decorator():\n    assert True\n",
            "decorator",
        ),
        (
            "import subprocess\nimport pytest\n\n@pytest.mark.docs\n"
            "def test_subprocess():\n    subprocess.run(['cat', 'docs/subprocess.md'])\n",
            "subprocess",
        ),
        (
            "import subprocess\nimport pytest\n\n@pytest.mark.docs\n"
            "def test_assigned_subprocess():\n"
            "    command = ['cat', 'docs/assigned-subprocess.md']\n"
            "    subprocess.run(command)\n",
            "assigned-subprocess",
        ),
    ]
    for source, label in cases:
        readers = _docs_reading_test_modules(source)
        _assert_exact_docs_markers(ast.parse(source), readers, f"{label}.py")
        mutated = source.replace("docs/", "config/", 1)
        with pytest.raises(AssertionError, match="must sit on exactly the readers"):
            _assert_exact_docs_markers(
                ast.parse(mutated), _docs_reading_test_modules(mutated), f"{label}-mutated.py"
            )

    (tmp_path / "plugin.py").write_text(
        "from pathlib import Path\nimport pytest\n\n@pytest.fixture\ndef plugin_path():\n"
        "    return Path('docs/plugin-mutation.md').read_text()\n"
    )
    plugin_source = (
        "pytest_plugins = 'plugin'\nimport pytest\n\n@pytest.mark.docs\n"
        "def test_plugin(plugin_path):\n    assert plugin_path\n"
    )
    _assert_exact_docs_markers(
        ast.parse(plugin_source),
        _docs_reading_test_modules(plugin_source, repository_root=tmp_path),
        "plugin.py",
    )
    (tmp_path / "plugin.py").write_text(
        (tmp_path / "plugin.py").read_text().replace("docs/", "config/", 1)
    )
    with pytest.raises(AssertionError, match="must sit on exactly the readers"):
        _assert_exact_docs_markers(
            ast.parse(plugin_source),
            _docs_reading_test_modules(plugin_source, repository_root=tmp_path),
            "plugin-mutated.py",
        )


#: D180 §2.3: every job that declares `needs: classify`, exactly.
_EXACT_CLASSIFY_CONSUMERS = frozenset(
    {
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
        "docs-fastpath",
        "codecov-upload",
        "checks",
        "web-unit-worker",
        "web",
        "web-snapshots-worker",
        "web-snapshots",
    }
)
_FULL_ONLY_WORKER_CONDITION = "needs.classify.outputs.mode != 'docs-only'"
_DOCS_ONLY_CONDITION = "needs.classify.outputs.mode == 'docs-only'"
_DOCS_FASTPATH_CONDITION = (
    "needs.classify.outputs.mode == 'docs-only' || needs.classify.outputs.mode == 'full'"
)
_GATE_JOBS = frozenset({"checks", "web", "web-snapshots"})
_FULL_ONLY_WORKERS = frozenset(
    {
        "quality",
        "pytest-ordinary",
        "pytest-serial",
        "pytest-stress",
        "package",
        "coverage",
        "web-unit-worker",
        "web-snapshots-worker",
    }
)


@pytest.mark.docs_ci
def test_ci_classifier_job_uses_closed_checkout_settings_and_base_trusted_execution() -> None:
    """D180 §2.9 replacement guard: the classify job stays exactly as specified.

    Replaces the slice-1/2a inertness guard (no job may consume the
    classifier) with an exact-consumer guard: the set of jobs declaring
    ``needs: classify`` equals the D180 §2.3 table exactly, each carries the
    exact literal condition string, the classify job's outputs mapping and
    head-checkout settings remain byte-identical, and the classifier step's
    run string is the base-trusted form.
    """

    loaded: object = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    jobs = cast(dict[str, dict[str, object]], loaded["jobs"])
    classify = jobs["classify"]
    assert cast(dict[str, str], classify["outputs"]) == {
        "mode": "${{ steps.classify.outputs.mode }}"
    }
    steps = cast(list[dict[str, object]], classify["steps"])
    assert len(steps) == 3
    head_checkout, base_checkout, classifier_step = steps
    assert head_checkout["uses"] == "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    assert head_checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert base_checkout["uses"] == "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    assert cast(dict[str, object], base_checkout["with"])["path"] == ".ci-gate-base"
    assert cast(dict[str, object], base_checkout["with"])["persist-credentials"] is False
    assert classifier_step["run"] == (
        "python3 .ci-gate-base/scripts/ci_change_classifier.py "
        '--event-name "${{ github.event_name }}" '
        '--base-sha "${{ github.event.pull_request.base.sha }}" '
        '--head-sha "${{ github.event.pull_request.head.sha }}"'
    )

    actual_consumers: set[str] = set()
    for name, job in jobs.items():
        if name == "classify":
            continue
        needs = job.get("needs", [])
        needs_values = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
        if "classify" in needs_values:
            actual_consumers.add(name)
        if name in _GATE_JOBS:
            assert job.get("if") == "always()", f"{name}: gate condition must be exactly always()"
        elif name in _FULL_ONLY_WORKERS:
            assert job.get("if") == _FULL_ONLY_WORKER_CONDITION, f"{name}: wrong worker condition"
        elif name == "docs-fastpath":
            assert job.get("if") == _DOCS_FASTPATH_CONDITION, (
                "docs-fastpath: wrong worker condition"
            )
        elif name == "codecov-upload":
            assert job.get("if") == "always()", "codecov-upload: wrong worker condition"

    assert actual_consumers == _EXACT_CLASSIFY_CONSUMERS
