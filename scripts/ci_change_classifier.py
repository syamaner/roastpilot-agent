"""Classify a pull request diff as closed docs-only or full CI work.

The classifier is deliberately local and fail-closed.  It receives immutable
pull-request commit identifiers from the workflow, interrogates only the local
Git object database, and emits a closed mode value for later workflow slices.
Slice 1 does not let any CI job consume that value.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from enum import Enum
from pathlib import Path


class ChangeMode(Enum):
    """Closed classifier outcomes for pull-request changes."""

    FULL = "full"
    DOCS_ONLY = "docs-only"


_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DOCS_PATH_PATTERN = re.compile(r"^docs/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md$")
_SINGLE_PATH_STATUSES = frozenset({"A", "M", "D"})
_PAIR_STATUS_PATTERN = re.compile(r"^[RC][0-9]{3}$")
_REGULAR_FILE_MODES = frozenset({b"100644", b"100755"})

#: Bounded per-call and total classifier worktime (D180 §2.7, B4-ii). Neither
#: is environment-configurable: a PR cannot widen its own classifier budget.
_GIT_CALL_TIMEOUT_SECONDS = 20.0
_TOTAL_BUDGET_SECONDS = 60.0


class _BudgetExceeded(Exception):
    """Internal signal that the total classifier worktime budget elapsed."""


def _run_git(arguments: Sequence[str], *, deadline: float | None = None) -> bytes:
    """Run one local Git command and return its standard output.

    Args:
        arguments: Git arguments excluding the executable name.
        deadline: Optional ``time.monotonic()`` instant that caps both Git
            process start and its timeout. Raises :class:`_BudgetExceeded`
            when no positive budget remains, before spawning a process.

    Returns:
        The command's byte-for-byte standard output.

    Raises:
        subprocess.CalledProcessError: If Git exits unsuccessfully.
        subprocess.TimeoutExpired: If the call exceeds the per-call timeout.
        OSError: If Git cannot be executed.
        _BudgetExceeded: If the total worktime budget has already elapsed.
    """

    timeout = _GIT_CALL_TIMEOUT_SECONDS
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExceeded
        timeout = min(timeout, remaining)
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return completed.stdout


def _is_docs_markdown_path(path: bytes) -> bool:
    """Return whether a Git path is within the closed docs Markdown grammar."""

    try:
        decoded = path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    if _DOCS_PATH_PATTERN.fullmatch(decoded) is None:
        return False
    return all(component not in {".", ".."} for component in decoded.split("/"))


def _parse_name_status(output: bytes) -> tuple[tuple[str, tuple[bytes, ...]], ...] | None:
    """Parse Git's NUL-delimited ``--name-status`` output under a closed grammar."""

    if not output or not output.endswith(b"\0"):
        return None
    fields = output[:-1].split(b"\0")
    entries: list[tuple[str, tuple[bytes, ...]]] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii")
        except UnicodeDecodeError:
            return None
        index += 1
        if status in _SINGLE_PATH_STATUSES:
            if index >= len(fields):
                return None
            entries.append((status, (fields[index],)))
            index += 1
        elif _PAIR_STATUS_PATTERN.fullmatch(status) is not None:
            if index + 1 >= len(fields):
                return None
            entries.append((status, (fields[index], fields[index + 1])))
            index += 2
        else:
            return None
    return tuple(entries)


def _regular_file_mode(commit: str, path: bytes, *, deadline: float | None = None) -> bytes | None:
    """Return a regular-file mode for ``path`` at ``commit``, else ``None``.

    This separate local object check rejects symlinks and submodules, which
    ``git diff --name-status`` intentionally does not describe.
    """

    decoded_path = path.decode("utf-8", errors="strict")
    output = _run_git(["ls-tree", "-z", commit, "--", decoded_path], deadline=deadline)
    expected_suffix = b"\t" + path + b"\0"
    if not output.endswith(expected_suffix):
        return None
    metadata = output[: -len(expected_suffix)].split(b" ")
    if len(metadata) != 3 or metadata[0] not in _REGULAR_FILE_MODES or metadata[1] != b"blob":
        return None
    return metadata[0]


def _is_regular_file(commit: str, path: bytes, *, deadline: float | None = None) -> bool:
    """Return whether ``path`` is a regular file at ``commit``."""

    return _regular_file_mode(commit, path, deadline=deadline) is not None


def _entries_are_docs_only(
    entries: Sequence[tuple[str, tuple[bytes, ...]]],
    merge_base: str,
    head_sha: str,
    *,
    deadline: float | None = None,
) -> bool:
    """Return whether non-empty status entries are all safe docs regular files."""

    if not entries:
        return False
    for status, paths in entries:
        if not all(_is_docs_markdown_path(path) for path in paths):
            return False
        if status == "A":
            if not _is_regular_file(head_sha, paths[0], deadline=deadline):
                return False
        elif status == "M":
            base_mode = _regular_file_mode(merge_base, paths[0], deadline=deadline)
            head_mode = _regular_file_mode(head_sha, paths[0], deadline=deadline)
            if base_mode is None or head_mode is None or base_mode != head_mode:
                return False
        elif status == "D":
            if not _is_regular_file(merge_base, paths[0], deadline=deadline):
                return False
        else:
            old_path, new_path = paths
            base_mode = _regular_file_mode(merge_base, old_path, deadline=deadline)
            head_mode = _regular_file_mode(head_sha, new_path, deadline=deadline)
            if base_mode is None or head_mode is None or base_mode != head_mode:
                return False
    return True


def classify_change(event_name: str, base_sha: str, head_sha: str) -> ChangeMode:
    """Classify an exact pull-request comparison, failing closed on every error.

    Every local Git call is bounded by a per-call timeout
    (:data:`_GIT_CALL_TIMEOUT_SECONDS`) and the whole classification is bounded
    by a total worktime budget (:data:`_TOTAL_BUDGET_SECONDS`).
    Neither bound is environment-configurable.

    Args:
        event_name: GitHub event name supplied by the workflow.
        base_sha: Exact lowercase base commit SHA from the pull request.
        head_sha: Exact lowercase head commit SHA from the pull request.

    Returns:
        ``DOCS_ONLY`` only for a non-empty closed-grammar Markdown-only diff;
        otherwise ``FULL``.
    """

    if (
        event_name != "pull_request"
        or not _SHA_PATTERN.fullmatch(base_sha)
        or not _SHA_PATTERN.fullmatch(head_sha)
    ):
        return ChangeMode.FULL
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    try:
        _run_git(["cat-file", "-e", f"{base_sha}^{{commit}}"], deadline=deadline)
        _run_git(["cat-file", "-e", f"{head_sha}^{{commit}}"], deadline=deadline)
        merge_base = (
            _run_git(["merge-base", base_sha, head_sha], deadline=deadline).decode("ascii").strip()
        )
        if _SHA_PATTERN.fullmatch(merge_base) is None:
            return ChangeMode.FULL
        diff = _run_git(
            ["diff", "-z", "--name-status", "--find-renames", "--no-color", merge_base, head_sha],
            deadline=deadline,
        )
        entries = _parse_name_status(diff)
        if entries is None:
            return ChangeMode.FULL
        if _entries_are_docs_only(entries, merge_base, head_sha, deadline=deadline):
            return ChangeMode.DOCS_ONLY
    except Exception:
        return ChangeMode.FULL
    return ChangeMode.FULL


def _parse_arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    """Parse the classifier's closed workflow input surface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    return parser.parse_args(arguments)


def _write_output(mode: ChangeMode, output_path: str | None) -> None:
    """Append exactly one closed mode line to GitHub Actions' output file."""

    if output_path is None:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"mode={mode.value}\n")


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the classifier without raising through the workflow boundary.

    Args:
        arguments: Optional command-line arguments, excluding the executable.

    Returns:
        Zero after emitting the closed result when possible.
    """

    try:
        parsed = _parse_arguments(arguments)
        mode = classify_change(parsed.event_name, parsed.base_sha, parsed.head_sha)
        _write_output(mode, os.environ.get("GITHUB_OUTPUT"))
    except (Exception, SystemExit):
        with suppress(Exception):
            _write_output(ChangeMode.FULL, os.environ.get("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
