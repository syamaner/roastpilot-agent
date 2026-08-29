"""Real-Git integration tests for the docs-only classifier (B5(i)).

These build throwaway repositories with real ``git init``/``commit`` under
``tmp_path`` and call :func:`ci_change_classifier.classify_change` against
real objects, so the merge-base (three-dot) semantics, rename/mode-bit
tracking, and symlink/gitlink rejection are exercised against Git itself
rather than a hand-authored ``--name-status`` transcript. Every test spawns a
real ``git`` subprocess, so the module is ``slow``-marked.

``classify_change`` (like the production workflow's checkout) assumes the
current working directory is the repository under inspection, so each test
temporarily ``chdir``s into its throwaway repository via
:func:`_current_working_directory` and always restores the original
directory, including on failure.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import ci_change_classifier as classifier
import pytest

pytestmark = pytest.mark.slow


@contextlib.contextmanager
def _current_working_directory(path: Path) -> Generator[None]:
    """Temporarily change the process working directory to ``path``."""

    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def _git(repo: Path, *arguments: str) -> str:
    """Run one real Git command in ``repo`` and return its stripped stdout."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    """Initialise a throwaway repository with a deterministic identity."""

    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.email", "rp702@example.invalid")
    _git(repo, "config", "user.name", "RP702 Real-Git Fixture")


def _commit(repo: Path, message: str) -> str:
    """Stage everything and commit, returning the new commit SHA."""

    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_docs_only_across_a_genuine_merge_base_with_base_advancing_independently(
    tmp_path: Path,
) -> None:
    """Three-dot semantics: an independent base-branch commit is never diffed against."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("original\n")
    (repo / "README.md").write_text("root\n")
    base = _commit(repo, "seed")

    _git(repo, "checkout", "--quiet", "-b", "feature")
    (repo / "docs" / "guide.md").write_text("edited\n")
    head = _commit(repo, "edit docs")

    _git(repo, "checkout", "--quiet", "main")
    (repo / "README.md").write_text("root, advanced independently\n")
    _commit(repo, "advance main independently")

    with _current_working_directory(repo):
        assert (
            classifier.classify_change("pull_request", base, head)
            is classifier.ChangeMode.DOCS_ONLY
        )


def test_real_git_mv_rename_inside_docs_is_docs_only(tmp_path: Path) -> None:
    """A genuine `git mv` rename that stays inside `docs/` is docs-only."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "old.md").write_text("content\n" * 20)
    base = _commit(repo, "seed")

    _git(repo, "mv", "docs/old.md", "docs/new.md")
    head = _commit(repo, "rename inside docs")

    with _current_working_directory(repo):
        assert (
            classifier.classify_change("pull_request", base, head)
            is classifier.ChangeMode.DOCS_ONLY
        )


def test_real_git_mv_rename_out_of_docs_is_full(tmp_path: Path) -> None:
    """A rename whose destination leaves `docs/` is full."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "old.md").write_text("content\n" * 20)
    base = _commit(repo, "seed")

    _git(repo, "mv", "docs/old.md", "old.md")
    head = _commit(repo, "rename out of docs")

    with _current_working_directory(repo):
        assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_real_symlink_at_a_docs_path_is_full(tmp_path: Path) -> None:
    """A real symlink whose path matches the grammar is still full."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "real.md").write_text("target\n")
    base = _commit(repo, "seed")

    (repo / "docs" / "link.md").symlink_to("real.md")
    head = _commit(repo, "add a docs-path symlink")

    with _current_working_directory(repo):
        assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_real_submodule_gitlink_entry_is_full(tmp_path: Path) -> None:
    """A real submodule (gitlink) entry under `docs/` is full."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("content\n")
    base = _commit(repo, "seed")

    submodule = tmp_path / "submodule"
    _init_repo(submodule)
    (submodule / "file.txt").write_text("sub\n")
    _commit(submodule, "submodule seed")

    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(submodule),
        "docs/nested.md",
    )
    head = _commit(repo, "add a gitlink at a docs/*.md path")

    with _current_working_directory(repo):
        assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_unrelated_history_with_no_merge_base_is_full(tmp_path: Path) -> None:
    """Two genuinely unrelated histories (no merge base) resolve to full."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("content\n")
    base = _commit(repo, "seed")

    _git(repo, "checkout", "--quiet", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", "--quiet", ".")
    (repo / "docs").mkdir()
    (repo / "docs" / "other.md").write_text("other\n")
    head = _commit(repo, "unrelated root commit")

    with _current_working_directory(repo):
        assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_real_hundred_percent_docs_only_history_of_three_commits_is_docs_only(
    tmp_path: Path,
) -> None:
    """A genuine multi-commit, all-docs history resolves to docs-only."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("v1\n")
    (repo / "README.md").write_text("root\n")
    base = _commit(repo, "seed")

    (repo / "docs" / "guide.md").write_text("v2\n")
    _commit(repo, "docs edit 1")
    (repo / "docs" / "second.md").write_text("new\n")
    _commit(repo, "docs edit 2")
    (repo / "docs" / "guide.md").write_text("v3\n")
    head = _commit(repo, "docs edit 3")

    with _current_working_directory(repo):
        assert (
            classifier.classify_change("pull_request", base, head)
            is classifier.ChangeMode.DOCS_ONLY
        )


def test_real_mode_bit_only_change_on_a_docs_file_is_full(tmp_path: Path) -> None:
    """A pure `100644` -> `100755` mode-bit flip on a docs file is full."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").mkdir()
    docs_file = repo / "docs" / "guide.md"
    docs_file.write_text("content\n")
    base = _commit(repo, "seed")

    docs_file.chmod(0o755)
    _git(repo, "update-index", "--chmod=+x", "docs/guide.md")
    head = _commit(repo, "mode-bit-only change")

    with _current_working_directory(repo):
        assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL
