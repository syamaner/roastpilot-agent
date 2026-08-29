"""Behavioural and structural tests for the inert docs-only CI classifier."""

from __future__ import annotations

import ast
import runpy
import subprocess
import sys
from collections.abc import Iterable, Sequence
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

    def fake_git(arguments: Iterable[str]) -> bytes:
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
            {"check": True, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL},
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

    def unexpected_tree(_arguments: Sequence[str]) -> bytes:
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

    def not_regular(_commit: str, _path: bytes) -> bool:
        return False

    monkeypatch.setattr(classifier, "_is_regular_file", not_regular)
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


def _docs_reading_test_modules(source: str) -> set[str]:
    """Return test-function names that execute a direct committed Markdown read.

    This intentionally walks executable calls only: comments and docstrings do
    not create a call node and cannot satisfy or evade the governance rule.
    """

    tree = ast.parse(source)
    module_aliases: set[str] = set()

    def expression_is_docs_markdown(expression: ast.expr, aliases: set[str]) -> bool:
        if isinstance(expression, ast.Name):
            return expression.id in aliases
        values = [node.value for node in ast.walk(expression) if isinstance(node, ast.Constant)]
        strings = [value for value in values if isinstance(value, str)]
        return any(value == "docs" or value.startswith("docs/") for value in strings) and any(
            value.endswith(".md") for value in strings
        )

    def expression_has_docs_root(expression: ast.expr) -> bool:
        """Return whether an expression contains the literal docs directory component."""

        return any(
            isinstance(node, ast.Constant) and node.value == "docs" for node in ast.walk(expression)
        )

    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if value is not None and expression_is_docs_markdown(value, module_aliases):
                for target in targets:
                    if isinstance(target, ast.Name):
                        module_aliases.add(target.id)

    readers: set[str] = set()
    for statement in tree.body:
        if not isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not statement.name.startswith("test_"):
            continue
        aliases = set(module_aliases)
        has_docs_markdown_glob = False
        for node in ast.walk(statement):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if expression_is_docs_markdown(node.value, aliases):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            aliases.add(target.id)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"glob", "rglob"}
                and expression_has_docs_root(node.func.value)
                and any(
                    isinstance(argument, ast.Constant) and argument.value == "*.md"
                    for argument in node.args
                )
            ):
                has_docs_markdown_glob = True
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"read_text", "read_bytes", "open"}:
                continue
            if expression_is_docs_markdown(node.func.value, aliases) or (
                has_docs_markdown_glob and isinstance(node.func.value, ast.Name)
            ):
                readers.add(statement.name)
    return readers


def _has_module_docs_marker(tree: ast.Module) -> bool:
    """Return whether a module registers the docs marker at module scope."""

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


def test_docs_reading_tests_are_marked_and_comments_do_not_count() -> None:
    """All executable committed-Markdown readers have the registered docs marker."""

    markdown_readers: dict[Path, set[str]] = {}
    for path in sorted((_REPO / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        readers = _docs_reading_test_modules(source)
        if readers:
            markdown_readers[path] = readers
            assert _has_module_docs_marker(ast.parse(source)), (
                f"{path.name}: missing pytest.mark.docs"
            )

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


def test_docs_governance_detects_a_hidden_unmarked_markdown_read() -> None:
    """The AST rule catches a direct Markdown read even without a path variable name."""

    source = (
        "from pathlib import Path\n\n"
        "def test_hidden() -> None:\n"
        '    Path("docs/hidden.md").read_text()\n'
    )
    assert _docs_reading_test_modules(source) == {"test_hidden"}
    assert not _has_module_docs_marker(ast.parse(source))


def test_ci_classifier_job_is_inert_and_uses_closed_checkout_settings() -> None:
    """No workflow job may consume the slice-1 classifier output yet."""

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
    checkout = steps[0]
    assert checkout["uses"] == "actions/checkout@v6.0.2"
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    classifier_step = steps[1]
    assert classifier_step["run"] == (
        'python scripts/ci_change_classifier.py --event-name "${{ github.event_name }}" '
        '--base-sha "${{ github.event.pull_request.base.sha }}" '
        '--head-sha "${{ github.event.pull_request.head.sha }}"'
    )
    for name, job in jobs.items():
        if name == "classify":
            continue
        needs = job.get("needs", [])
        needs_values = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
        assert "classify" not in needs_values, f"{name} must not depend on the inert classifier"
        assert "classify.outputs" not in yaml.safe_dump(job), f"{name} consumes classifier output"
