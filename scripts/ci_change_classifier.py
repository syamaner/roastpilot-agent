"""Classify a pull request diff as closed docs-only or full CI work.

The classifier is deliberately local and fail-closed.  It receives immutable
pull-request commit identifiers from the workflow, interrogates only the local
Git object database, and emits a closed mode value for later workflow slices.
Slice 2 lets the ``docs-fastpath``/gate-worker split consume that value (see
``.github/workflows/ci.yml``); the classifier itself stays exactly as fail-
closed as slice 1 made it.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from enum import Enum
from pathlib import Path

#: Per-Git-call subprocess timeout (B5(ii)). A literal, never environment-
#: readable: an attacker-influenced environment must not be able to widen
#: the bound this classifier fails closed against.
_GIT_CALL_TIMEOUT_SECONDS = 20.0

#: Total wall-clock budget for one ``classify_change`` invocation (B5(ii)).
#: Checked before every Git call so a sequence of individually-under-timeout
#: calls cannot run unbounded.
_TOTAL_BUDGET_SECONDS = 60.0


class ChangeMode(Enum):
    """Closed classifier outcomes for pull-request changes."""

    FULL = "full"
    DOCS_ONLY = "docs-only"


_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DOCS_PATH_PATTERN = re.compile(r"^docs/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md$")
_SINGLE_PATH_STATUSES = frozenset({"A", "M", "D"})
_PAIR_STATUS_PATTERN = re.compile(r"^[RC][0-9]{3}$")
_REGULAR_FILE_MODES = frozenset({b"100644", b"100755"})


def _run_git(arguments: Sequence[str], *, deadline: float = math.inf) -> bytes:
    """Run one local Git command and return its standard output.

    Args:
        arguments: Git arguments excluding the executable name.
        deadline: A ``time.monotonic()`` instant this call must not start
            after (B5(ii)). Defaults to no bound, so every existing direct
            caller and test keeps its current two-argument call shape;
            :func:`classify_change` is the only caller that supplies a real,
            budget-derived deadline.

    Returns:
        The command's byte-for-byte standard output.

    Raises:
        TimeoutError: If ``deadline`` has already passed.
        subprocess.CalledProcessError: If Git exits unsuccessfully.
        subprocess.TimeoutExpired: If Git exceeds the per-call timeout.
        OSError: If Git cannot be executed.
    """

    remaining_seconds = deadline - time.monotonic()
    if remaining_seconds <= 0:
        raise TimeoutError("classifier total budget exceeded before this Git call")
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=min(_GIT_CALL_TIMEOUT_SECONDS, remaining_seconds),
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


def _regular_file_mode(commit: str, path: bytes, *, deadline: float = math.inf) -> bytes | None:
    """Return ``path``'s exact regular-file mode at ``commit``, or ``None``.

    This separate local object check rejects symlinks and submodules, which
    ``git diff --name-status`` intentionally does not describe, and exposes
    the exact mode bytes so a caller can additionally require the mode to be
    unchanged (a pure ``100644`` -> ``100755`` mode-bit flip is a real
    change, not "docs-only" — see the M6 mutation and its real-Git test).

    Args:
        commit: The commit-ish to inspect ``path`` at.
        path: The exact Git path, as raw bytes.
        deadline: See :func:`_run_git`.

    Returns:
        The exact mode bytes (e.g. ``b"100644"``) if ``path`` is a regular
        file at ``commit``; ``None`` otherwise.
    """

    decoded_path = path.decode("utf-8", errors="strict")
    output = _run_git(["ls-tree", "-z", commit, "--", decoded_path], deadline=deadline)
    expected_suffix = b"\t" + path + b"\0"
    if not output.endswith(expected_suffix):
        return None
    metadata = output[: -len(expected_suffix)].split(b" ")
    if len(metadata) == 3 and metadata[0] in _REGULAR_FILE_MODES and metadata[1] == b"blob":
        return metadata[0]
    return None


def _entries_are_docs_only(
    entries: Sequence[tuple[str, tuple[bytes, ...]]],
    merge_base: str,
    head_sha: str,
    *,
    deadline: float = math.inf,
) -> bool:
    """Return whether non-empty status entries are all safe docs regular files.

    For ``M`` (modify) and rename/copy statuses, the mode is required to be
    an unchanged regular-file mode at both endpoints: a pure mode-bit change
    on an otherwise-untouched docs file is a real change, not docs-only.
    """

    if not entries:
        return False
    for status, paths in entries:
        if not all(_is_docs_markdown_path(path) for path in paths):
            return False
        if status == "A":
            if _regular_file_mode(head_sha, paths[0], deadline=deadline) is None:
                return False
        elif status == "M":
            base_mode = _regular_file_mode(merge_base, paths[0], deadline=deadline)
            head_mode = _regular_file_mode(head_sha, paths[0], deadline=deadline)
            if base_mode is None or head_mode is None or base_mode != head_mode:
                return False
        elif status == "D":
            if _regular_file_mode(merge_base, paths[0], deadline=deadline) is None:
                return False
        else:
            old_path, new_path = paths
            old_mode = _regular_file_mode(merge_base, old_path, deadline=deadline)
            new_mode = _regular_file_mode(head_sha, new_path, deadline=deadline)
            if old_mode is None or new_mode is None or old_mode != new_mode:
                return False
    return True


def classify_change(event_name: str, base_sha: str, head_sha: str) -> ChangeMode:
    """Classify an exact pull-request comparison, failing closed on every error.

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
    # B5(ii): the deadline is established once, immediately before the first
    # Git call, and every subsequent call (including the ones nested inside
    # `_entries_are_docs_only`/`_is_regular_file`) is checked against it.
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
