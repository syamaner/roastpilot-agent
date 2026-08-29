"""Behavioural and structural tests for the inert docs-only CI classifier."""

from __future__ import annotations

import ast
import runpy
import subprocess
import sys
import time
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

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"local-output")

    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
    assert classifier._run_git(["merge-base", _BASE, _HEAD]) == b"local-output"  # pyright: ignore[reportPrivateUsage]
    assert calls == [
        (
            ["git", "merge-base", _BASE, _HEAD],
            {
                "check": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "timeout": classifier._GIT_CALL_TIMEOUT_SECONDS,  # pyright: ignore[reportPrivateUsage]
            },
        )
    ]


@pytest.mark.docs_ci
def test_run_git_caps_a_near_deadline_call_to_the_remaining_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started Git call cannot outlive the classifier's remaining total budget."""

    observed: list[float] = []

    def fake_run(_arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append(cast(float, kwargs["timeout"]))
        return subprocess.CompletedProcess([], 0, stdout=b"")

    monkeypatch.setattr(classifier.time, "monotonic", lambda: 59.5)
    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
    classifier._run_git(["status"], deadline=60.0)  # pyright: ignore[reportPrivateUsage]
    assert observed == [0.5]


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
    """A per-call ``TimeoutExpired`` (already swallowed broadly) still resolves FULL.

    The observed ``timeout=`` kwarg is captured outside the classifier's own
    broad exception handler (an in-fixture assertion failure would itself be
    swallowed to FULL and silently pass regardless of the real value — this
    would make an M18 mutation, dropping ``timeout=`` from ``_run_git``, go
    undetected).
    """

    observed_timeouts: list[object] = []

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=arguments, timeout=20.0)

    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    assert observed_timeouts
    assert observed_timeouts[0] == classifier._GIT_CALL_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]


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

    def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    monkeypatch.setattr(classifier.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
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


def _is_docs_markdown_glob_call(node: ast.AST, root_aliases: set[str]) -> bool:
    """Return whether a call is a ``*.md`` glob/rglob rooted at ``docs/``.

    ``root_aliases`` names any variable already known to carry a docs-rooted
    directory (full or partial evidence alike — a directory root never itself
    ends in ``.md``, so partial evidence of the ``docs`` component is already
    conclusive proof for a glob root).
    """

    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _GLOB_METHOD_NAMES
        and any(
            isinstance(argument, ast.Constant) and argument.value == "*.md"
            for argument in node.args
        )
    ):
        return False
    root = node.func.value
    if isinstance(root, ast.Name):
        return root.id in root_aliases
    return _expression_has_docs_root(root)


def _is_pytest_fixture_decorator(decorator: ast.expr) -> bool:
    """Return whether a decorator expression is (a call to) ``pytest.fixture``."""

    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr == "fixture"
    if isinstance(target, ast.Name):
        return target.id == "fixture"
    return False


@dataclass
class _FunctionAnalysis:
    """One same-module function's direct docs-read status and call edges."""

    reads_directly: bool
    calls: set[str]
    unresolved: list[str]


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
    known_functions: set[str],
    filename: str,
    class_methods: dict[str, str] | None = None,
    local_functions: dict[str, str] | None = None,
    ambiguous_local_functions: set[str] | None = None,
) -> _FunctionAnalysis:
    """Walk one function body for a direct docs read, calls, and unresolved reads.

    A read receiver is "unresolved" when it has partial evidence the closed
    rule cannot prove either way: it folds a literal ``docs`` component but
    no provable ``.md``-suffixed segment, so it might be hiding a
    non-literal docs read the rule cannot otherwise trace.
    """

    aliases = set(module_aliases)
    partial_aliases: set[str] = set()
    calls: set[str] = set()
    unresolved: list[str] = []
    has_docs_glob = False
    reads = False

    def _receiver_is_docs(target: ast.expr) -> bool:
        if isinstance(target, ast.Name):
            return target.id in aliases or has_docs_glob
        return _expression_is_docs_markdown(target, aliases)

    def _receiver_is_partial(target: ast.expr) -> bool:
        if isinstance(target, ast.Name):
            return target.id in partial_aliases
        return _expression_has_docs_root(target)

    nodes: list[ast.AST] = list(func.body) if not isinstance(func, ast.Lambda) else [func.body]
    while nodes:
        node = nodes.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        nodes.extend(ast.iter_child_nodes(node))
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            full_match = _expression_is_docs_markdown(node.value, aliases)
            partial_match = not full_match and _expression_has_docs_root(node.value)
            for target in targets:
                if isinstance(target, ast.Name):
                    if full_match:
                        aliases.add(target.id)
                        partial_aliases.discard(target.id)
                    elif partial_match:
                        partial_aliases.add(target.id)
                    else:
                        aliases.discard(target.id)
                        partial_aliases.discard(target.id)
        docs_root_aliases = aliases | partial_aliases
        if isinstance(node, ast.For) and _is_docs_markdown_glob_call(node.iter, docs_root_aliases):
            has_docs_glob = True
            if isinstance(node.target, ast.Name):
                aliases.add(node.target.id)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in node.generators:
                if _is_docs_markdown_glob_call(generator.iter, docs_root_aliases) and isinstance(
                    generator.target, ast.Name
                ):
                    aliases.add(generator.target.id)
        if _is_docs_markdown_glob_call(node, docs_root_aliases):
            has_docs_glob = True
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id == "open" and node.args:
                target = node.args[0]
                if _receiver_is_docs(target):
                    reads = True
                elif _receiver_is_partial(target):
                    unresolved.append(
                        f"{filename}:{node.lineno}: `open(...)` receiver folds a 'docs' "
                        "component with no provable '.md' segment — cannot prove this is "
                        "or is not a docs read"
                    )
            elif node.func.id in (local_functions or {}):
                calls.add((local_functions or {})[node.func.id])
            elif node.func.id in (ambiguous_local_functions or set()):
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved local helper edge `{node.func.id}()`"
                )
            elif node.func.id in known_functions:
                calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHOD_NAMES:
            target = node.func.value
            if _receiver_is_docs(target):
                reads = True
            elif _receiver_is_partial(target):
                unresolved.append(
                    f"{filename}:{node.lineno}: `.{node.func.attr}()` receiver folds a "
                    "'docs' component with no provable '.md' segment — cannot prove this "
                    "is or is not a docs read"
                )
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"self", "cls"}
        ):
            callee = (class_methods or {}).get(node.func.attr)
            if callee is None:
                unresolved.append(
                    f"{filename}:{node.lineno}: unresolved same-class call edge "
                    f"`{node.func.value.id}.{node.func.attr}()`"
                )
            else:
                calls.add(callee)
    return _FunctionAnalysis(reads_directly=reads, calls=calls, unresolved=unresolved)


def _docs_reading_test_modules(source: str, filename: str = "<module>") -> set[str]:
    """Return the exact executable test names that read committed ``docs/**/*.md``.

    A test counts when it (or a same-module helper/fixture it calls,
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
    module_aliases: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if value is not None and _expression_is_docs_markdown(value, module_aliases):
                for target in targets:
                    if isinstance(target, ast.Name):
                        module_aliases.add(target.id)

    functions: dict[str, _FunctionNode] = {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    class_methods: dict[str, dict[str, str]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        members = [
            member
            for member in statement.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if not any(member.name.startswith("test_") for member in members):
            continue
        methods: dict[str, str] = {}
        for member in members:
            qualified_name = f"{statement.name}::{member.name}"
            functions[qualified_name] = member
            methods[member.name] = qualified_name
        class_methods[statement.name] = methods
    fixtures = {
        name
        for name, node in functions.items()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_pytest_fixture_decorator(decorator) for decorator in node.decorator_list)
    }
    known_functions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    local_targets: dict[str, dict[str, str]] = {}
    ambiguous_local_targets: dict[str, set[str]] = {}
    local_keys: set[str] = set()
    for owner, node in list(functions.items()):
        helpers, ambiguous = _local_helpers(node)
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
            known_functions,
            filename,
            class_methods.get(class_name) if class_name else None,
            local_targets.get(name),
            ambiguous_local_targets.get(name),
        )
        analyses[name] = analysis
        if name not in local_keys:
            unresolved.extend(analysis.unresolved)
    if unresolved:
        raise AssertionError(
            "docs-content governance found an unresolved dynamic read receiver that "
            f"could hide an unmarked docs read: {unresolved}"
        )

    def reads_transitively(name: str, seen: frozenset[str]) -> bool:
        if name in seen or name not in analyses or len(seen) >= _MAX_DOCS_CALL_GRAPH_DEPTH:
            return False
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
        param_names = [argument.arg for argument in node.args.args]
        fixture_edges = {param for param in param_names if param in fixtures}
        if reads_transitively(name, frozenset()) or any(
            reads_transitively(fixture_name, frozenset()) for fixture_name in fixture_edges
        ):
            readers.add(name)
    return readers


def _function_has_docs_marker(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether a function carries the exact ``@pytest.mark.docs`` decorator."""

    return any(ast.unparse(decorator) == "pytest.mark.docs" for decorator in node.decorator_list)


def _marked_docs_test_prefixes(tree: ast.Module) -> set[str]:
    """Return exact test prefixes carrying a method- or class-level docs marker."""

    marked: set[str] = set()
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
    return marked


def _assert_exact_docs_markers(tree: ast.Module, readers: set[str], filename: str) -> None:
    """Fail when exact docs-reader prefixes and effective docs markers differ."""

    marked = _marked_docs_test_prefixes(tree)
    assert marked == readers, (
        f"{filename}: @pytest.mark.docs must sit on exactly the readers "
        f"{sorted(readers)}, found {sorted(marked)}"
    )


def _module_has_pytestmark_docs(tree: ast.Module) -> bool:
    """Return whether a module still registers the retired module-level docs marker."""

    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        if ast.unparse(statement.value) == "pytest.mark.docs":
            return True
    return False


def _test_module_paths(tests_root: Path) -> list[Path]:
    """Return every pytest-style test module below ``tests_root`` in stable order."""

    return sorted(tests_root.rglob("test_*.py"))


@pytest.mark.docs_ci
def test_docs_reading_tests_carry_the_exact_docs_marker_and_nothing_else() -> None:
    """Every committed-Markdown reader is marked at the exact function, module-wide."""

    markdown_readers: dict[Path, set[str]] = {}
    for path in _test_module_paths(_REPO / "tests"):
        relative_path = path.relative_to(_REPO).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not _module_has_pytestmark_docs(tree), (
            f"{relative_path}: retired module-level `pytestmark = pytest.mark.docs` still present"
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
    """Nested pytest modules remain visible to the exact marker self-audit."""

    tests_root = tmp_path / "tests"
    nested = tests_root / "nested"
    nested.mkdir(parents=True)
    (tests_root / "test_top_level.py").write_text("def test_top() -> None: pass\n")
    (nested / "test_nested.py").write_text("def test_nested() -> None: pass\n")
    (nested / "helper.py").write_text("pass\n")
    assert [path.relative_to(tests_root).as_posix() for path in _test_module_paths(tests_root)] == [
        "nested/test_nested.py",
        "test_top_level.py",
    ]


@pytest.mark.docs_ci
def test_docs_governance_detects_a_hidden_unmarked_markdown_read() -> None:
    """The AST rule catches a direct Markdown read even without a path variable name."""

    source = (
        "from pathlib import Path\n\n"
        "def test_hidden() -> None:\n"
        '    Path("docs/hidden.md").read_text()\n'
    )
    assert _docs_reading_test_modules(source) == {"test_hidden"}
    assert not _module_has_pytestmark_docs(ast.parse(source))


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
            '    for entry in Path("docs").glob("*.md"):\n'
            "        entry.read_text()\n",
            {"test_a"},
            id="glob-star-md",
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
            '    texts = [entry.read_text() for entry in Path("docs").glob("*.md")]\n'
            "    assert texts\n",
            {"test_a"},
            id="list-comprehension-docs-glob",
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
    assert head_checkout["uses"] == "actions/checkout@v6.0.2"
    assert head_checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert base_checkout["uses"] == "actions/checkout@v6.0.2"
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

    assert actual_consumers == _EXACT_CLASSIFY_CONSUMERS
