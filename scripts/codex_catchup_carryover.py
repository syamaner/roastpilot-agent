"""Decide whether a Codex clean verdict carries over a catch-up merge (D241).

A head change that only merges the base branch into a PR branch leaves the
PR's reviewed code untouched. D241 lets a bot-authored CLEAN Codex verdict on
the previous head ``A`` carry to the new head ``B`` only when this script
proves, from git alone, that:

1. ``B`` is a two-parent merge commit whose first parent is ``A``;
2. ``B``'s second parent is an ancestor of the base ref (base history only);
3. ``B``'s tree is EXACTLY the tree git produces for a clean automatic merge
   of ``A`` and that second parent (``git merge-tree --write-tree``). Any edit
   made during the merge (whitespace, a moved line, a conflict resolution, a
   binary or mode change) changes the tree and fails the proof; a conflicting
   merge has no automatic tree and fails too.

Anything it cannot prove exits non-zero, and the caller re-triggers Codex. A
catch-up chained onto an earlier catch-up (first parent is not ``A``) also
re-triggers. It never decides about findings verdicts, which never carry.
Requires git 2.38 or later.

Usage::

    python scripts/codex_catchup_carryover.py --old A --new B [--base origin/main]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence


def _git(*args: str) -> str:
    """Run git and return stdout, raising on failure."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def carryover_reason(old: str, new: str, base: str) -> str | None:
    """Why the verdict cannot carry over, or ``None`` when it can.

    Args:
        old: The head the clean verdict names.
        new: The current PR head.
        base: The base ref the PR targets (for example ``origin/main``).

    Returns:
        ``None`` when the verdict carries over; otherwise a short reason.
    """
    parents = _git("rev-list", "--parents", "-n", "1", new).split()[1:]
    if len(parents) != 2:
        return "new head is not a two-parent merge commit"
    old_full = _git("rev-parse", "--verify", f"{old}^{{commit}}").strip()
    if parents[0] != old_full:
        return "new head's first parent is not the verified head"
    probe = subprocess.run(
        ["git", "merge-base", "--is-ancestor", parents[1], base], capture_output=True
    )
    if probe.returncode != 0:
        return "merged-in parent is not base history"
    merge = subprocess.run(
        ["git", "merge-tree", "--write-tree", old_full, parents[1]],
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        return "the automatic merge of the verified head and base conflicts"
    automatic_tree = merge.stdout.split()[0]
    if automatic_tree != _git("rev-parse", f"{new}^{{tree}}").strip():
        return "new head is not the automatic merge of the verified head and base"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Print the decision; exit 0 to carry the verdict over, 1 otherwise.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when the clean verdict carries over, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description="Decide whether a Codex clean verdict carries over a catch-up merge (D241)."
    )
    parser.add_argument("--old", required=True, help="head the clean verdict names")
    parser.add_argument("--new", required=True, help="current PR head")
    parser.add_argument("--base", default="origin/main", help="base ref the PR targets")
    args = parser.parse_args(argv)
    try:
        reason = carryover_reason(args.old, args.new, args.base)
    except subprocess.CalledProcessError as exc:
        reason = f"git failed: {' '.join(exc.cmd)}"
    if reason is None:
        print(f"CARRY: {args.new} only merges {args.base} into {args.old}")
        return 0
    print(f"RE-TRIGGER: {reason}")
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
