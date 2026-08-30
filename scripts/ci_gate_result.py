"""Fail closed when declared CI jobs do not match their required outcomes.

The classifier is a producer that swallows uncertainty into ``FULL``.  This
helper is a gate, so its polarity is the opposite: any malformed input,
unexpected result, or internal exception exits non-zero.  It is valid on the
local Python 3.11 floor and on GitHub Actions' newer default ``python3``.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from typing import cast

_VALID_MODES = frozenset({"full", "docs-only"})
_VALID_RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})
_CLASSES = ("always", "full-only", "docs-only")
_MANIFESTS: dict[str, dict[str, tuple[str, ...]]] = {
    "checks": {
        "always": ("classify", "docs-fastpath", "codecov-upload"),
        "full-only": (
            "quality",
            "pytest-ordinary",
            "pytest-serial",
            "pytest-stress",
            "package",
            "coverage",
        ),
        "docs-only": (),
    },
    "web": {
        "always": ("classify",),
        "full-only": ("web-unit-worker",),
        "docs-only": (),
    },
    "web-snapshots": {
        "always": ("classify",),
        "full-only": ("web-snapshots-worker",),
        "docs-only": (),
    },
}


def _parse_arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    """Parse the frozen, repeatable job-class command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    for job_class in _CLASSES:
        parser.add_argument(f"--{job_class}", action="append", default=[])
    return parser.parse_args(arguments)


def _failure(mode: str | None, rows: Sequence[tuple[str, str, str]]) -> int:
    """Print a bounded diagnostic table and return the failing exit status."""

    resolved_mode = mode if mode else "<missing>"
    print(f"mode={resolved_mode}")
    print("job\texpected\tactual")
    for job, expected, actual in rows:
        print(f"{job}\t{expected}\t{actual}")
    return 1


def _declared_jobs(namespace: argparse.Namespace) -> dict[str, tuple[str, ...]]:
    """Return the parsed command-line classes without trusting them as authority."""

    return {
        job_class: tuple(getattr(namespace, job_class.replace("-", "_"))) for job_class in _CLASSES
    }


def _manifest_jobs(
    job_name: str | None,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]] | None, tuple[str, str, str] | None]:
    """Return the fixed manifest for one protected gate or a closed identity error."""

    if not job_name:
        return {}, None, ("<configuration>", "known GITHUB_JOB", "missing")
    manifest = _MANIFESTS.get(job_name)
    if manifest is None:
        return {}, None, ("<configuration>", "known GITHUB_JOB", job_name)

    declared: dict[str, str] = {}
    for job_class in _CLASSES:
        for job_id in manifest[job_class]:
            if job_id in declared:
                return {}, None, ("<configuration>", "unique manifest job id", job_id)
            declared[job_id] = job_class
    if not declared:
        return {}, None, ("<configuration>", "non-empty manifest", job_name)
    return declared, manifest, None


def _expected_result(mode: str, job_class: str) -> str:
    """Return the one permitted result for a declared job class and mode."""

    if job_class == "always":
        return "success"
    if job_class == "full-only":
        return "success" if mode == "full" else "skipped"
    return "skipped" if mode == "full" else "success"


def _load_needs(
    raw_needs: str | None,
) -> tuple[dict[str, object] | None, tuple[str, str, str] | None]:
    """Decode the closed Actions needs object without echoing its raw contents."""

    if not raw_needs:
        return None, ("<needs>", "non-empty JSON object", "missing or empty")
    try:
        decoded: object = json.loads(raw_needs)
    except json.JSONDecodeError:
        return None, ("<needs>", "JSON object", "invalid JSON")
    if not isinstance(decoded, dict):
        return None, ("<needs>", "JSON object", type(decoded).__name__)
    return cast(dict[str, object], decoded), None


def _evaluate(
    namespace: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[bool, tuple[tuple[str, str, str], ...]]:
    """Evaluate every declared and received job under the closed gate contract."""

    mode = environment.get("MODE")
    if mode not in _VALID_MODES:
        return False, (("<mode>", "full or docs-only", mode or "<missing>"),)
    declared, manifest, manifest_error = _manifest_jobs(environment.get("GITHUB_JOB"))
    if manifest_error is not None or manifest is None:
        return False, (manifest_error,) if manifest_error is not None else ()
    if _declared_jobs(namespace) != manifest:
        return False, (("<configuration>", "exact manifest argv", "inconsistent argv"),)
    needs, needs_error = _load_needs(environment.get("NEEDS_JSON"))
    if needs_error is not None or needs is None:
        return False, (needs_error,) if needs_error is not None else ()

    rows: list[tuple[str, str, str]] = []
    for job_id, entry in needs.items():
        if not isinstance(entry, dict):
            rows.append((job_id, "object with string result", type(entry).__name__))
            continue
        result: object = cast(dict[str, object], entry).get("result")
        if not isinstance(result, str):
            rows.append((job_id, "string result", type(result).__name__))
            continue
        if result not in _VALID_RESULTS:
            rows.append((job_id, "success, failure, cancelled, or skipped", result))
            continue
        job_class = declared.get(job_id)
        if job_class is None:
            rows.append((job_id, "declared job", result))
            continue
        expected = _expected_result(mode, job_class)
        if result != expected:
            rows.append((job_id, expected, result))

    for job_id, job_class in declared.items():
        if job_id not in needs:
            rows.append((job_id, _expected_result(mode, job_class), "missing"))
    return not rows, tuple(rows)


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run the gate and return zero only for the exact declared success matrix.

    Args:
        arguments: Optional command-line arguments excluding the executable.
        environment: Optional environment mapping, mainly for deterministic tests.

    Returns:
        Zero when all declared jobs match the current closed-mode expectations;
        otherwise one.
    """

    active_environment = os.environ if environment is None else environment
    mode = active_environment.get("MODE")
    try:
        namespace = _parse_arguments(arguments)
        accepted, rows = _evaluate(namespace, active_environment)
        if accepted:
            return 0
        return _failure(mode, rows)
    except Exception:
        return _failure(mode, (("<internal>", "successful evaluation", "exception"),))


if __name__ == "__main__":
    raise SystemExit(main())
