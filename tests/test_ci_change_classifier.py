"""Behavioural and structural tests for the docs-only CI change classifier.

Slice 2 (#702) lets ``ci.yml``'s gate/worker split consume
``classify_change``'s verdict; the workflow-structure proof that the verdict
is consumed correctly lives in ``tests/test_pytest_governance.py`` (which
already owns the rest of the CI-workflow structural assertions). This module
keeps the classifier's own behavioural, bounded-worktime, and docs-reader
governance coverage.
"""

from __future__ import annotations

import ast
import math
import runpy
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import ci_change_classifier as classifier
import pytest
from docs_reader_governance import UnresolvedDocsReaderEdge, docs_reading_tests

# The whole module is fast, hardware-free, and IS the classifier's own focused
# test suite (never a docs/**/*.md content reader itself) — the docs-only CI
# fast path (#702) runs it to trust the mechanism that grants its own
# shortcut. The real-Git integration coverage lives in the separate `slow`
# tests/test_ci_change_classifier_real_git.py, deliberately excluded here to
# keep the fast path fast.
pytestmark = pytest.mark.docs_ci

_REPO = Path(__file__).resolve().parents[1]
_BASE = "a" * 40
_HEAD = "b" * 40
_MERGE_BASE = "c" * 40


def _name_status(*fields: bytes) -> bytes:
    """Build a NUL-delimited Git ``--name-status`` payload for one test."""

    return b"\0".join(fields) + b"\0"


def _regular_tree_entry(path: bytes) -> bytes:
    """Build a regular-file ``git ls-tree -z`` record for ``path``."""

    return b"100644 blob " + (b"d" * 40) + b"\t" + path + b"\0"


def _install_git_fixture(
    monkeypatch: pytest.MonkeyPatch,
    diff: bytes,
    *,
    regular_paths: set[tuple[str, bytes]] | None = None,
) -> list[tuple[str, ...]]:
    """Install a deterministic local Git transcript and return its command log."""

    calls: list[tuple[str, ...]] = []
    admitted = regular_paths

    def fake_git(arguments: Iterable[str], *, deadline: float = math.inf) -> bytes:
        del deadline  # the timeout/budget behaviour has its own dedicated tests below
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
                return _regular_tree_entry(path)
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

    def unexpected_tree(_arguments: Sequence[str], *, deadline: float = math.inf) -> bytes:
        del deadline
        return b"100644 blob deadbeef\tother.md\0"

    monkeypatch.setattr(classifier, "_run_git", unexpected_tree)
    assert classifier._regular_file_mode(_HEAD, b"docs/safe.md") is None  # pyright: ignore[reportPrivateUsage]


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

    def never_regular(_commit: str, _path: bytes, *, deadline: float = math.inf) -> bytes | None:
        del deadline
        return None

    monkeypatch.setattr(classifier, "_regular_file_mode", never_regular)
    assert not classifier._entries_are_docs_only([entry], _MERGE_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "entry",
    [
        ("M", (b"docs/safe.md",)),
        ("R100", (b"docs/old.md", b"docs/new.md")),
    ],
)
def test_mode_change_between_endpoints_is_full_even_when_both_are_regular(
    monkeypatch: pytest.MonkeyPatch, entry: tuple[str, tuple[bytes, ...]]
) -> None:
    """A pure mode-bit flip (both endpoints individually regular) is never docs-only."""

    def mode_at(commit: str, _path: bytes, *, deadline: float = math.inf) -> bytes | None:
        del deadline
        return b"100644" if commit == _MERGE_BASE else b"100755"

    monkeypatch.setattr(classifier, "_regular_file_mode", mode_at)
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

    def fake_git(arguments: Iterable[str]) -> bytes:
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

    def fake_git(arguments: Iterable[str]) -> bytes:
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


def test_bounded_worktime_constants_are_literals_not_environment_derived() -> None:
    """B5(ii): both budgets are plain module-level literals, never env-readable."""

    assert classifier._GIT_CALL_TIMEOUT_SECONDS == 20.0  # pyright: ignore[reportPrivateUsage]
    assert classifier._TOTAL_BUDGET_SECONDS == 60.0  # pyright: ignore[reportPrivateUsage]
    source = Path(classifier.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source.split("_TOTAL_BUDGET_SECONDS")[0].splitlines()[-1]


def test_total_budget_exceeded_between_calls_is_full_and_stops_further_git_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B5(ii): an exhausted total budget is FULL, and no further Git call is issued (M2).

    This exercises the REAL ``_run_git`` (only the lower ``subprocess.run``
    layer is faked), so it genuinely proves the deadline check inside
    ``_run_git`` itself — deleting that check (M2) would let this test's
    second, budget-exceeding Git call proceed instead of being refused.
    """

    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    monkeypatch.setattr(classifier.subprocess, "run", fake_run)

    # Reading 1 establishes the deadline in `classify_change`. Reading 2 is
    # the in-budget check before the FIRST `_run_git` call. Every reading
    # after that has already advanced past the budget, so the SECOND
    # `_run_git` call must raise before ever reaching `subprocess.run`.
    readings = iter([0.0, 0.0, 1_000.0])

    def fake_monotonic() -> float:
        return next(readings, 1_000.0)

    monkeypatch.setattr(classifier.time, "monotonic", fake_monotonic)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "cat-file", "-e"]


def test_subprocess_timeout_expired_resolves_to_full_not_docs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: a raw `TimeoutExpired` from Git must never be mistaken for DOCS_ONLY."""

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        raise subprocess.TimeoutExpired(cmd=arguments, timeout=1.0)

    monkeypatch.setattr(classifier.subprocess, "run", fake_run)
    assert classifier.classify_change("pull_request", _BASE, _HEAD) is classifier.ChangeMode.FULL


# ---------------------------------------------------------------------------
# Docs-reader governance (B5(iii)): the shared, importable
# `docs_reader_governance` module is the single authoritative implementation,
# also consumed by `tests/test_pytest_governance.py`'s node-id equality
# assertion. These tests exercise its non-literal-construction detection and
# its fail-closed ambiguity handling directly.
# ---------------------------------------------------------------------------

#: The exact, hand-audited reader inventory for the five modules that read
#: committed docs/**/*.md content, cross-checked against the shared
#: analyzer's own output below (never asserted from the analyzer alone).
_KNOWN_READERS: dict[str, frozenset[str]] = {
    "test_agent_model_pins.py": frozenset({"test_topology_reference_table_rows_match_the_map"}),
    "test_agent_worktree_controls.py": frozenset({"test_runbook_citations_never_use_line_anchors"}),
    "test_config.py": frozenset(
        {"test_runbook_recovery_off_describes_the_d88_ceiling_not_heat_direction"}
    ),
    "test_capture_agent_usage.py": frozenset(
        {
            "test_provisioning_docs_set_bytecode_variables_on_both_pip_commands",
            "test_runbook_and_skill_and_agents_row_point_to_print_validation_commands",
            "test_native_usage_and_evidence_collection_contracts_are_documented",
            "test_bound_root_literal_sweep_matches_only_expected_sites",
        }
    ),
}


def test_docs_reading_tests_match_the_hand_audited_inventory() -> None:
    """The shared analyzer's output equals the hand-audited reader set, file by file."""

    for filename, expected in _KNOWN_READERS.items():
        source = (_REPO / "tests" / filename).read_text(encoding="utf-8")
        assert docs_reading_tests(source) == expected, filename

    # `test_worktree_gate_recipe.py` reads the runbook through a same-module
    # helper (`_load_recipe`) that nearly every test in the module calls;
    # only the three that provably never reach it are excluded.
    recipe_source = (_REPO / "tests" / "test_worktree_gate_recipe.py").read_text(encoding="utf-8")
    non_readers = {
        "test_mutation_g4_exists_based_guard_fails_open_on_dangling_symlink",
        "test_mutation_g6_bare_rebuild_remedy_fails_t14",
        "test_mutation_g8_heading_rename_fails_t20",
    }
    readers = docs_reading_tests(recipe_source)
    assert readers.isdisjoint(non_readers)
    assert len(readers) == 29


def _committed_docs_marked_tests(source: str) -> set[str]:
    """Return the exact `test_` function names carrying `@pytest.mark.docs` in ``source``.

    Module-level ``pytestmark = pytest.mark.docs`` is deliberately NOT
    resolved here: slice 2 requires exact per-function markers for every
    genuine docs/**/*.md reader (only the separate, non-reading `docs_ci`
    marker may still be applied at module scope).
    """

    tree = ast.parse(source)
    marked: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in statement.decorator_list:
            if ast.unparse(decorator) == "pytest.mark.docs":
                marked.add(statement.name)
    return marked


def test_committed_docs_markers_exactly_match_the_governance_derived_readers() -> None:
    """M25: every governance-derived reader is marked, and nothing else is.

    This is the link the marker-removal mutation actually breaks: unlike
    `docs_reading_tests` (which only inspects read expressions, never
    decorators), this test inspects the *committed* `@pytest.mark.docs`
    decorators and requires them to equal the independently re-derived
    reader inventory for every module known to read docs/**/*.md content.
    """

    for filename in (*_KNOWN_READERS, "test_worktree_gate_recipe.py"):
        source = (_REPO / "tests" / filename).read_text(encoding="utf-8")
        assert _committed_docs_marked_tests(source) == docs_reading_tests(source), filename


def test_no_unrelated_test_module_is_classified_as_a_docs_reader() -> None:
    """Every `tests/test_*.py` outside the five known modules has zero readers."""

    known = set(_KNOWN_READERS) | {"test_worktree_gate_recipe.py", "test_ci_change_classifier.py"}
    for path in sorted((_REPO / "tests").glob("test_*.py")):
        if path.name in known:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            readers = docs_reading_tests(source)
        except UnresolvedDocsReaderEdge as error:  # pragma: no cover - fails loudly by design
            raise AssertionError(f"{path.name}: {error}") from error
        assert not readers, f"{path.name}: unexpected docs reader(s) {readers}"


def test_no_module_still_uses_the_broad_module_level_docs_marker() -> None:
    """Slice 2 replaces every broad `pytestmark = pytest.mark.docs` with exact markers."""

    for path in sorted((_REPO / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            targets = {target.id for target in statement.targets if isinstance(target, ast.Name)}
            if "pytestmark" in targets:
                assert ast.unparse(statement.value) != "pytest.mark.docs", (
                    f"{path.name}: still uses the broad module-level docs marker"
                )


def test_comments_and_docstrings_never_count_as_a_docs_read() -> None:
    """A docs-looking string inside prose or a docstring creates no reader."""

    assert (
        docs_reading_tests(
            "def test_comment() -> None:\n"
            '    """docs/only-a-docstring.md"""\n'
            "    # docs/comment.md\n"
            "    assert True\n"
        )
        == set()
    )


def test_docs_governance_detects_a_hidden_unmarked_markdown_read() -> None:
    """The rule catches a direct Markdown read even without a path variable name."""

    source = (
        "from pathlib import Path\n\n"
        "def test_hidden() -> None:\n"
        '    Path("docs/hidden.md").read_text()\n'
    )
    assert docs_reading_tests(source) == {"test_hidden"}


@pytest.mark.parametrize(
    ("label", "positive", "negative"),
    [
        (
            "joinpath",
            "from pathlib import Path\n"
            "ROOT = Path('/repo')\n"
            "def test_reads() -> None:\n"
            "    ROOT.joinpath('docs', 'guide.md').read_text()\n",
            "from pathlib import Path\n"
            "ROOT = Path('/repo')\n"
            "def test_reads() -> None:\n"
            "    ROOT.joinpath('other', 'guide.md').read_text()\n",
        ),
        (
            "os_path_join",
            "import os\n"
            "def test_reads(stem: str) -> None:\n"
            "    open(os.path.join('docs', stem + '.md')).read()\n",
            "import os\n"
            "def test_reads(stem: str) -> None:\n"
            "    open(os.path.join('other', stem + '.md')).read()\n",
        ),
        (
            "f_string",
            "def test_reads(name: str) -> None:\n    open(f'docs/{name}.md').read()\n",
            "def test_reads(name: str) -> None:\n    open(f'other/{name}.md').read()\n",
        ),
        (
            "str_format",
            "def test_reads(name: str) -> None:\n    open('docs/{}.md'.format(name)).read()\n",
            "def test_reads(name: str) -> None:\n    open('other/{}.txt'.format(name)).read()\n",
        ),
        (
            "builtin_open",
            "from pathlib import Path\n"
            "def test_reads() -> None:\n"
            "    open(Path('docs') / 'guide.md').read()\n",
            "from pathlib import Path\n"
            "def test_reads() -> None:\n"
            "    open(Path('other') / 'guide.md').read()\n",
        ),
        (
            "glob_over_slash_built_root",
            "from pathlib import Path\n"
            "ROOT = Path('/repo')\n"
            "def test_reads() -> None:\n"
            "    paths = [*sorted((ROOT / 'docs').rglob('*.md')), ROOT / 'AGENTS.md']\n"
            "    for path in paths:\n"
            "        path.read_text()\n",
            "from pathlib import Path\n"
            "ROOT = Path('/repo')\n"
            "def test_reads() -> None:\n"
            "    paths = [*sorted((ROOT / 'other').rglob('*.md')), ROOT / 'AGENTS.md']\n"
            "    for path in paths:\n"
            "        path.read_text()\n",
        ),
        (
            "loop_variable_alias",
            "from pathlib import Path\n"
            "def test_reads() -> None:\n"
            "    for name in ('docs/a.md', 'docs/b.md'):\n"
            "        Path(name).read_text()\n",
            "from pathlib import Path\n"
            "def test_reads() -> None:\n"
            "    for name in ('other/a.md', 'other/b.md'):\n"
            "        Path(name).read_text()\n",
        ),
        (
            "same_module_helper",
            "from pathlib import Path\n"
            "def _read_it() -> str:\n"
            "    return Path('docs/help.md').read_text()\n"
            "def test_reads() -> None:\n"
            "    assert _read_it()\n",
            "from pathlib import Path\n"
            "def _read_it() -> str:\n"
            "    return Path('other/help.md').read_text()\n"
            "def test_reads() -> None:\n"
            "    assert _read_it()\n",
        ),
        (
            "pytest_fixture",
            "import pytest\n"
            "from pathlib import Path\n"
            "@pytest.fixture\n"
            "def runbook() -> str:\n"
            "    return Path('docs/runbook.md').read_text()\n"
            "def test_reads(runbook: str) -> None:\n"
            "    assert runbook\n",
            "import pytest\n"
            "from pathlib import Path\n"
            "@pytest.fixture\n"
            "def runbook() -> str:\n"
            "    return Path('other/runbook.md').read_text()\n"
            "def test_reads(runbook: str) -> None:\n"
            "    assert runbook\n",
        ),
    ],
)
def test_non_literal_construction_forms_are_detected_and_not_false_positive(
    label: str, positive: str, negative: str
) -> None:
    """Each B5(iii) construction form has a real positive and a real negative."""

    assert docs_reading_tests(positive) == {"test_reads"}, label
    assert docs_reading_tests(negative) == set(), label


def test_unresolved_dynamic_call_edge_fails_the_governance_test_with_a_named_reason() -> None:
    """A docs-rooted-but-unprovable read raises rather than guessing or broad-marking."""

    source = (
        "from pathlib import Path\n"
        "DOCS_DIR = Path('docs')\n"
        "def test_ambiguous() -> None:\n"
        "    DOCS_DIR.read_text()\n"
    )
    with pytest.raises(UnresolvedDocsReaderEdge, match="test_ambiguous"):
        docs_reading_tests(source)
