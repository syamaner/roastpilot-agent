"""Decide whether a Codex clean verdict carries over a catch-up merge (D241).

A head change that only merges the base branch into a PR branch leaves the
PR's own change set unchanged. D241 lets a bot-authored CLEAN Codex verdict on
the previous head ``A`` carry to the new head ``B`` only when this script
proves, from git alone, that:

1. ``B`` is a merge commit whose first parent is ``A``;
2. ``B``'s second parent is an ancestor of the base ref (it brings in base
   history, nothing else); and
3. the PR's own change set is identical: the stable patch-id of
   ``diff(merge-base(base, A), A)`` equals that of
   ``diff(merge-base(base, B), B)``, computed with zero context so unrelated
   base changes that only shift line numbers do not count.

Anything it cannot prove exits non-zero, and the caller re-triggers Codex.
It never decides about findings verdicts, which do not carry over.

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


def _patch_id(base: str, head: str) -> str | None:
    """The stable patch-id of the PR's own changes, or ``None`` if empty."""
    merge_base = _git("merge-base", base, head).strip()
    diff = _git("diff", "--no-color", "--no-ext-diff", "-U0", merge_base, head)
    if not diff.strip():
        return None
    out = subprocess.run(
        ["git", "patch-id", "--stable"], input=diff, check=True, capture_output=True, text=True
    ).stdout.split()
    return out[0] if out else None


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
    old_id, new_id = _patch_id(base, old), _patch_id(base, new)
    if old_id is None or new_id is None:
        return "empty change set"
    if old_id != new_id:
        return "the PR's own change set differs"
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
