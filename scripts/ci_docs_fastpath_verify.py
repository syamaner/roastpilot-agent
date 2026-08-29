"""Independently re-verify the docs-only verdict inside the fast-path job itself.

``classify`` runs once, in its own job, and hands its verdict to every other
job through ``needs.classify.outputs.mode``. This script closes the "single
job's output becomes authoritative on its own word" gap for the docs-only
path specifically: ``docs-fastpath`` re-derives the verdict from the real Git
objects in *its own* checkout (not a copy of the classifier's in-memory
result) and fails the job — and therefore the required ``Checks`` gate — on
any disagreement.

It imports :func:`ci_change_classifier.classify_change` directly (one
authoritative copy of the grammar, per D154) rather than re-implementing any
part of it, and adds exactly one deliberately **narrowing** redundancy: a
second, independent recomputation of the changed path set via
``git diff --name-only``, required to be non-empty and every name required to
match ``docs/**/*.md`` textually. This redundancy can only fail closed — it
is never permitted to admit a path the classifier itself rejected — because
it exists to catch a re-verification-time drift (a different checkout state,
a race, a non-determinism) rather than to weaken the grammar.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence

from ci_change_classifier import ChangeMode, classify_change

#: Bounded diagnostic budget: never print the full rejected-path list, only a
#: count and this many example names (slice-1 output-hygiene rule, carried
#: forward here for the same reason: no attacker-influenced byte stream is
#: ever echoed at length into a public log).
_MAX_REPORTED_PATHS = 5

#: Timeout for the narrowing redundancy's own Git subprocess call.
_GIT_TIMEOUT_SECONDS = 20.0


class DocsFastpathVerificationError(Exception):
    """Raised for any condition that must fail the docs-fastpath job closed."""


def _recomputed_docs_only_paths(base_sha: str, head_sha: str) -> tuple[str, ...]:
    """Recompute the merge-base changed-path set independently of the classifier.

    Args:
        base_sha: The pull request's base commit SHA.
        head_sha: The pull request's head commit SHA.

    Returns:
        Every changed path name, in Git's reported order.

    Raises:
        DocsFastpathVerificationError: If Git fails, times out, the output
            cannot be decoded, or no paths changed.
    """

    try:
        merge_base = (
            subprocess.run(
                ["git", "merge-base", base_sha, head_sha],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
            .stdout.decode("ascii")
            .strip()
        )
        completed = subprocess.run(
            ["git", "diff", "-z", "--name-only", "--find-renames", merge_base, head_sha],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except UnicodeDecodeError as error:
        raise DocsFastpathVerificationError(
            f"independent merge-base is not valid ASCII: {error}"
        ) from error
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise DocsFastpathVerificationError(
            f"independent Git recomputation failed: {error}"
        ) from error
    raw = completed.stdout
    if not raw or not raw.endswith(b"\0"):
        raise DocsFastpathVerificationError("independent recomputation found no changed paths")
    try:
        names = tuple(name.decode("utf-8", errors="strict") for name in raw[:-1].split(b"\0"))
    except UnicodeDecodeError as error:
        raise DocsFastpathVerificationError(f"changed path is not valid UTF-8: {error}") from error
    if not names:  # pragma: no cover — bytes.split always yields >=1 element, even on b""
        raise DocsFastpathVerificationError("independent recomputation found no changed paths")
    return names


def _assert_paths_are_narrowly_docs_markdown(paths: Sequence[str]) -> None:
    """Fail unless every recomputed path matches the narrowing docs/*.md check.

    Args:
        paths: The independently recomputed changed-path names.

    Raises:
        DocsFastpathVerificationError: If any path is outside ``docs/`` or
            does not end in ``.md``. Never prints the unbounded path list.
    """

    rejected = [path for path in paths if not (path.startswith("docs/") and path.endswith(".md"))]
    if rejected:
        sample = ", ".join(rejected[:_MAX_REPORTED_PATHS])
        extra = len(rejected) - _MAX_REPORTED_PATHS
        more = "" if extra <= 0 else f" (+{extra} more)"
        raise DocsFastpathVerificationError(
            f"{len(rejected)} recomputed path(s) outside docs/**/*.md: {sample}{more}"
        )


def verify_docs_only(base_sha: str, head_sha: str) -> None:
    """Fail unless both the classifier and the narrowing redundancy agree.

    Args:
        base_sha: The pull request's base commit SHA.
        head_sha: The pull request's head commit SHA.

    Raises:
        DocsFastpathVerificationError: If the classifier's own re-run does
            not resolve to ``DOCS_ONLY``, or the narrowing redundancy finds
            any path outside ``docs/**/*.md``.
    """

    resolved = classify_change("pull_request", base_sha, head_sha)
    if resolved is not ChangeMode.DOCS_ONLY:
        raise DocsFastpathVerificationError(
            f"classifier re-verification resolved to {resolved.value!r}, not docs-only"
        )
    paths = _recomputed_docs_only_paths(base_sha, head_sha)
    _assert_paths_are_narrowly_docs_markdown(paths)


def _parse_arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    """Parse the verifier's closed workflow input surface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the re-verification and report a fail-closed exit code.

    Args:
        arguments: Command-line arguments, excluding the executable.

    Returns:
        ``0`` only when both the classifier and the narrowing redundancy
        agree the change is docs-only.
    """

    parsed = _parse_arguments(arguments)
    try:
        verify_docs_only(parsed.base_sha, parsed.head_sha)
    except DocsFastpathVerificationError as error:
        print(f"docs-fastpath re-verification failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # fail-closed backstop: a gate never fails open
        print(
            f"docs-fastpath re-verification failed with an unexpected "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
