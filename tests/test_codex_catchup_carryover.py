"""Tests for the D241 Codex catch-up carry-over check (real git repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import codex_catchup_carryover as carry


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main with two files; a PR branch that edits one of them."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    base = "".join(f"line {i}\n" for i in range(1, 21))
    _commit(tmp_path, "a.txt", base, "base a")
    _commit(tmp_path, "b.txt", "b\n", "base b")
    _git(tmp_path, "switch", "-q", "-c", "pr")
    _commit(tmp_path, "a.txt", base.replace("line 15\n", "line 15 changed\n"), "pr edit")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _advance_main(repo: Path, path: str, text: str) -> None:
    _git(repo, "switch", "-q", "main")
    _commit(repo, path, text, f"main edits {path}")
    _git(repo, "switch", "-q", "pr")


def _merge_main(repo: Path) -> str:
    _git(repo, "merge", "-q", "--no-edit", "main")
    return _git(repo, "rev-parse", "HEAD")


def test_pure_catch_up_merge_carries_the_verdict(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD")
    _advance_main(repo, "b.txt", "b changed on main\n")
    new = _merge_main(repo)
    assert carry.carryover_reason(old, new, "main") is None
    assert carry.main(["--old", old, "--new", new, "--base", "main"]) == 0


def test_main_shifting_lines_in_the_same_file_still_carries(repo: Path) -> None:
    """Base edits above the PR's hunk move line numbers, not the change set."""
    old = _git(repo, "rev-parse", "HEAD")
    base = "".join(f"line {i}\n" for i in range(1, 21))
    _advance_main(repo, "a.txt", "header\n" + base)
    new = _merge_main(repo)
    assert carry.carryover_reason(old, new, "main") is None


def test_a_new_pr_commit_needs_a_fresh_review(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "c.txt", "new work\n", "more pr work")
    assert carry.carryover_reason(old, new, "main") == ("new head is not a two-parent merge commit")


def test_a_merge_that_also_edits_needs_a_fresh_review(repo: Path) -> None:
    """An 'evil merge' slips extra changes into the merge commit."""
    old = _git(repo, "rev-parse", "HEAD")
    _advance_main(repo, "b.txt", "b changed on main\n")
    _git(repo, "merge", "-q", "--no-commit", "main")
    (repo / "c.txt").write_text("smuggled\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "--no-edit")
    new = _git(repo, "rev-parse", "HEAD")
    assert carry.carryover_reason(old, new, "main") == (
        "new head is not the automatic merge of the verified head and base"
    )


def test_first_parent_must_be_the_verified_head(repo: Path) -> None:
    verified = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "c.txt", "unreviewed\n", "unreviewed pr work")
    _advance_main(repo, "b.txt", "b changed on main\n")
    new = _merge_main(repo)
    assert carry.carryover_reason(verified, new, "main") == (
        "new head's first parent is not the verified head"
    )


def test_merged_in_parent_must_be_base_history(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "side", "main")
    _commit(repo, "side.txt", "side\n", "side work")
    _git(repo, "switch", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", "side")
    new = _git(repo, "rev-parse", "HEAD")
    assert carry.carryover_reason(old, new, "main") == "merged-in parent is not base history"


def _evil_merge(repo: Path, path: str, text: str) -> str:
    """Merge main but rewrite ``path`` before committing the merge."""
    _advance_main(repo, "b.txt", f"b changed on main for {path}\n")
    _git(repo, "merge", "-q", "--no-commit", "main")
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "--no-edit")
    return _git(repo, "rev-parse", "HEAD")


def test_whitespace_only_edit_in_a_merge_needs_a_fresh_review(repo: Path) -> None:
    """Adversarial review: dedenting a PR line changes Python control flow
    but not a whitespace-insensitive patch-id; the tree proof catches it."""
    old = _commit(repo, "m.py", "def f(x):\n    if x:\n        check()\n", "guarded call")
    new = _evil_merge(repo, "m.py", "def f(x):\n    if x:\n        pass\n    check()\n")
    assert carry.carryover_reason(old, new, "main") == (
        "new head is not the automatic merge of the verified head and base"
    )
    old2 = _commit(repo, "w.py", "x = 'rm -rf build'\n", "literal")
    new2 = _evil_merge(repo, "w.py", "x = 'rm -rfbuild'\n")
    assert carry.carryover_reason(old2, new2, "main") is not None


def test_moved_pr_line_in_a_merge_needs_a_fresh_review(repo: Path) -> None:
    """Adversarial review: a zero-context patch-id cannot see a hunk move."""
    body = "def f():\n    check()\n\n\ndef g():\n    pass\n"
    old = _commit(repo, "m.py", body, "check in f")
    moved = "def f():\n    pass\n\n\ndef g():\n    check()\n"
    new = _evil_merge(repo, "m.py", moved)
    assert carry.carryover_reason(old, new, "main") == (
        "new head is not the automatic merge of the verified head and base"
    )


def test_conflicting_catch_up_needs_a_fresh_review(repo: Path) -> None:
    """A hand-resolved conflict has no automatic merge to prove against."""
    old = _git(repo, "rev-parse", "HEAD")
    base = "".join(f"line {i}\n" for i in range(1, 21))
    _advance_main(repo, "a.txt", base.replace("line 15\n", "line 15 on main\n"))
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "main"], capture_output=True)
    (repo / "a.txt").write_text(base.replace("line 15\n", "line 15 resolved\n"), encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "--no-edit")
    new = _git(repo, "rev-parse", "HEAD")
    assert carry.carryover_reason(old, new, "main") == (
        "the automatic merge of the verified head and base conflicts"
    )


def test_chained_catch_up_needs_a_fresh_review(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD")
    _advance_main(repo, "b.txt", "b changed on main\n")
    _merge_main(repo)
    _advance_main(repo, "b.txt", "b changed again\n")
    new = _merge_main(repo)
    assert carry.carryover_reason(old, new, "main") == (
        "new head's first parent is not the verified head"
    )


def test_git_failure_is_a_re_trigger(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _advance_main(repo, "b.txt", "b changed on main\n")
    new = _merge_main(repo)
    assert carry.main(["--old", "deadbeef", "--new", new, "--base", "main"]) == 1
    assert capsys.readouterr().out.startswith("RE-TRIGGER: git failed")


def test_agents_md_rule_names_this_check_and_its_limits() -> None:
    """D241's rule and the script stay in step: the rule names the script, and
    a findings verdict never carries."""
    root = Path(__file__).resolve().parents[1]
    agents = " ".join((root / "AGENTS.md").read_text(encoding="utf-8").split())
    assert (root / "scripts/codex_catchup_carryover.py").is_file()
    assert "**Catch-up carry-over (D241" in agents
    assert "scripts/codex_catchup_carryover.py --old <verdict sha> --new <head>" in agents
    assert "A findings verdict never carries" in agents
    assert "Hosted CI still reruns on the new head" in agents
