"""Fail-closed aggregate gate over a mode-conditional set of workflow jobs.

``ci.yml``'s gate/worker split (slice 2, #702) means a required check can now
legitimately pass while one of its dependencies never ran: a ``full-only``
worker is ``skipped`` on a docs-only pull request, and ``docs-fastpath`` is
``skipped`` on every other pull request. This script is the single place
that decision is proven correct for one gate job, from the real
``needs.<id>.result`` values GitHub reports, never from a status function.

Unlike :mod:`ci_change_classifier`, whose "swallow every exception and emit
the safe FULL value" polarity is correct for a *producer* that must never
break the workflow, this script is a *gate*: its whole job is to fail loudly.
Every unexpected condition here — a malformed environment, an undeclared job,
an unknown result string — exits non-zero rather than silently returning
success, because a gate that fails open is a green wall over an unvalidated
merge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import cast

#: The two closed values GitHub can report for ``needs.classify.outputs.mode``.
_FULL_MODE = "full"
_DOCS_ONLY_MODE = "docs-only"
_CLOSED_MODES = frozenset({_FULL_MODE, _DOCS_ONLY_MODE})

#: The closed set of ``needs.<id>.result`` values GitHub Actions can report.
_CLOSED_RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})


class GateResultError(Exception):
    """Raised for any condition that must fail the gate closed."""


def _required_result(declared_class: str, mode: str) -> str:
    """Return the single acceptable ``result`` for a declared class in ``mode``.

    Args:
        declared_class: One of ``"always"``, ``"full-only"``, ``"docs-only"``.
        mode: The resolved classifier mode, already validated as closed.

    Returns:
        ``"success"`` or ``"skipped"`` per the closed decision table (§2.3).
    """

    if declared_class == "always":
        return "success"
    if declared_class == "full-only":
        return "success" if mode == _FULL_MODE else "skipped"
    # declared_class == "docs-only"
    return "success" if mode == _DOCS_ONLY_MODE else "skipped"


def evaluate_gate(
    mode: str,
    needs: dict[str, object],
    *,
    always: Sequence[str],
    full_only: Sequence[str],
    docs_only: Sequence[str],
) -> None:
    """Raise :class:`GateResultError` unless every declared job matches its state.

    Args:
        mode: The raw ``needs.classify.outputs.mode`` string.
        needs: The parsed ``toJSON(needs)`` mapping: job id to a mapping that
            must carry a string ``result`` field.
        always: Job ids that must be ``success`` in every mode.
        full_only: Job ids that must be ``success`` in full mode and
            ``skipped`` in docs-only mode.
        docs_only: Job ids that must be ``skipped`` in full mode and
            ``success`` in docs-only mode.

    Raises:
        GateResultError: On any fail-closed direction listed in the module
            docstring and contract §2.3.
    """

    if mode not in _CLOSED_MODES:
        raise GateResultError(f"classifier mode is not one of {sorted(_CLOSED_MODES)!r}: {mode!r}")

    declarations: dict[str, str] = {}
    for job_id in always:
        if job_id in declarations:
            raise GateResultError(f"job {job_id!r} declared in more than one class")
        declarations[job_id] = "always"
    for job_id in full_only:
        if job_id in declarations:
            raise GateResultError(f"job {job_id!r} declared in more than one class")
        declarations[job_id] = "full-only"
    for job_id in docs_only:
        if job_id in declarations:
            raise GateResultError(f"job {job_id!r} declared in more than one class")
        declarations[job_id] = "docs-only"
    if not declarations:
        raise GateResultError("no jobs were declared in any class")

    for job_id in declarations:
        if job_id not in needs:
            raise GateResultError(f"declared job {job_id!r} is absent from NEEDS_JSON")
    for job_id in needs:
        if job_id not in declarations:
            raise GateResultError(f"job {job_id!r} is present in NEEDS_JSON but not declared")

    mismatches: list[str] = []
    for job_id, declared_class in declarations.items():
        details = needs[job_id]
        if not isinstance(details, dict):
            raise GateResultError(f"job {job_id!r} has no string 'result' field")
        raw_result = cast(dict[str, object], details).get("result")
        if not isinstance(raw_result, str):
            raise GateResultError(f"job {job_id!r} has no string 'result' field")
        result = raw_result
        if result not in _CLOSED_RESULTS:
            raise GateResultError(f"job {job_id!r} reported an unknown result: {result!r}")
        expected = _required_result(declared_class, mode)
        if result != expected:
            mismatches.append(f"{job_id} ({declared_class}): expected {expected!r}, got {result!r}")

    if mismatches:
        raise GateResultError(f"mode={mode!r}: " + "; ".join(mismatches))


def _parse_arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    """Parse the gate's closed workflow input surface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--always", action="append", default=[])
    parser.add_argument("--full-only", action="append", default=[])
    parser.add_argument("--docs-only", action="append", default=[])
    return parser.parse_args(arguments)


def _load_needs(raw: str | None) -> dict[str, object]:
    """Parse the ``NEEDS_JSON`` environment payload under a closed grammar.

    Args:
        raw: The raw ``NEEDS_JSON`` environment value, or ``None`` if unset.

    Returns:
        The parsed JSON object.

    Raises:
        GateResultError: If ``raw`` is missing, empty, or not a JSON object.
    """

    if not raw:
        raise GateResultError("NEEDS_JSON is missing or empty")
    try:
        parsed = cast(object, json.loads(raw))
    except json.JSONDecodeError as error:
        raise GateResultError(f"NEEDS_JSON is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise GateResultError("NEEDS_JSON is not a JSON object")
    return cast(dict[str, object], parsed)


def main(arguments: Sequence[str] | None, environ: dict[str, str]) -> int:
    """Run the gate and report a fail-closed exit code.

    Args:
        arguments: Command-line arguments, excluding the executable.
        environ: The process environment (``NEEDS_JSON`` and ``MODE``).

    Returns:
        ``0`` only when every declared job matched its required state.
    """

    try:
        parsed = _parse_arguments(arguments)
        needs = _load_needs(environ.get("NEEDS_JSON"))
        mode = environ.get("MODE") or ""
        evaluate_gate(
            mode,
            needs,
            always=parsed.always,
            full_only=parsed.full_only,
            docs_only=parsed.docs_only,
        )
    except GateResultError as error:
        print(f"CI gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], dict(os.environ)))
