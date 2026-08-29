"""Real-Git integration proof for the docs-only classifier (D180 §2.7/§3.2, B4-i).

Every test here builds a throwaway repository with a REAL ``git`` subprocess
(``git init``, real commits, real merges) and calls
:func:`ci_change_classifier.classify_change` against real Git objects — no
monkeypatched ``_run_git``. This is deliberately independent of the
table-driven fake-Git suite in ``test_ci_change_classifier.py``: it proves
the classifier's real-Git integration, including genuine three-dot
merge-base semantics, real symlinks, real gitlinks, and real mode-bit
changes, none of which a scripted fake transcript can misrepresent by
construction. The classifier itself always runs `git` in the current
process working directory, so every test uses ``monkeypatch.chdir`` to point
it at the throwaway repository — no classifier internals are patched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import ci_change_classifier as classifier
import pytest

pytestmark = pytest.mark.slow

_ENV = {
    "GIT_AUTHOR_NAME": "RoastPilot Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "RoastPilot Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *arguments: str) -> str:
    """Run one real Git command in ``repo`` and return its trimmed stdout."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_ENV,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    """Initialize a real repository with a deterministic default branch."""

    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")


def _write(repo: Path, relative_path: str, content: str) -> None:
    """Write a real regular file inside the repository."""

    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    """Stage everything and create one real commit, returning its SHA."""

    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_docs_only_across_a_genuine_advancing_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three-dot semantics: the base branch advances independently of the PR branch."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "root\n")
    root = _commit(repo, "root")

    _git(repo, "checkout", "--quiet", "-b", "feature", root)
    _write(repo, "docs/note.md", "hello\n")
    head = _commit(repo, "docs: add note")

    _git(repo, "checkout", "--quiet", "main")
    _write(repo, "src/app.py", "print('unrelated')\n")
    base = _commit(repo, "unrelated main advance")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.DOCS_ONLY


def test_real_git_mv_inside_docs_is_docs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``git mv`` within ``docs/`` is a genuine rename Git detects."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "docs/old.md", "content\n")
    base = _commit(repo, "add docs/old.md")

    _git(repo, "mv", "docs/old.md", "docs/new.md")
    head = _commit(repo, "rename within docs")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.DOCS_ONLY


def test_real_rename_out_of_docs_is_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real rename whose destination leaves ``docs/`` fails closed."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "docs/old.md", "content\n")
    base = _commit(repo, "add docs/old.md")

    _git(repo, "mv", "docs/old.md", "moved.md")
    head = _commit(repo, "rename out of docs")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_a_real_symlink_at_a_grammar_matching_path_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real symlink object, even at a path the closed regex admits, fails closed."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "root\n")
    base = _commit(repo, "root")

    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "link.md").symlink_to("README.md")
    head = _commit(repo, "add a real docs symlink")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_a_real_gitlink_submodule_entry_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real gitlink (submodule) entry under ``docs/`` fails closed."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "root\n")
    base = _commit(repo, "root")

    submodule = tmp_path / "submodule"
    _init_repo(submodule)
    _write(submodule, "file.txt", "content\n")
    _commit(submodule, "submodule root")

    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule),
        "docs/sub.md",
    )
    head = _commit(repo, "add a real gitlink under docs")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_unrelated_histories_with_no_merge_base_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two repositories with no common ancestor cannot produce a merge base."""

    base_repo = tmp_path / "base"
    _init_repo(base_repo)
    _write(base_repo, "README.md", "base\n")
    base = _commit(base_repo, "base root")

    head_repo = tmp_path / "head"
    _init_repo(head_repo)
    _write(head_repo, "docs/note.md", "head\n")
    _commit(head_repo, "head root")

    _git(
        head_repo,
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--quiet",
        str(base_repo),
        f"{base}:refs/heads/imported-base",
    )
    head = _git(head_repo, "rev-parse", "HEAD")

    monkeypatch.chdir(head_repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL


def test_a_genuine_three_commit_docs_only_history_is_docs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real multi-commit docs-only history stays docs-only end to end."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "root\n")
    base = _commit(repo, "root")

    _write(repo, "docs/one.md", "one\n")
    _commit(repo, "docs: add one")
    _write(repo, "docs/two.md", "two\n")
    _commit(repo, "docs: add two")
    _write(repo, "docs/one.md", "one, revised\n")
    head = _commit(repo, "docs: revise one")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.DOCS_ONLY


def test_a_real_mode_bit_only_change_on_a_docs_file_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``100644`` -> ``100755`` docs transition fails closed."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "docs/note.md", "content\n")
    base = _commit(repo, "add docs/note.md")

    (repo / "docs" / "note.md").chmod(0o755)
    _git(repo, "add", "-A")
    head = _commit(repo, "mode-bit-only change")

    monkeypatch.chdir(repo)
    assert classifier.classify_change("pull_request", base, head) is classifier.ChangeMode.FULL
