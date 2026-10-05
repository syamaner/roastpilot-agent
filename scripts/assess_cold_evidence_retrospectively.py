"""Offline D211 technical assessment; never establishes physical qualification.

Use an external seal receipt, not a digest derived from the input manifest.
The output is separate from sealed evidence and contains closed outcomes and
content digests only. Supplied artifact/review digests are assertions, not proof
that a wheel was installed or that independent physical gates passed.
"""

import argparse
import json
import os
from pathlib import Path
from typing import NoReturn

from roastpilot_agent.cold_characterisation.evidence_reader import read_retained_run_v6
from roastpilot_agent.cold_characterisation.temperature_conformance import (
    check_current_conformance,
    check_revised_conformance,
)


class AssessmentRefusedError(RuntimeError):
    """Sanitised refusal with no retained-input values."""

    def __init__(self) -> None:
        """Create the fixed refusal."""
        super().__init__("Offline assessment refused.")


def _digest(value: str, length: int = 64) -> str:
    """Admit an exact lowercase content digest."""
    if (
        type(value) is not str
        or len(value) != length
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise AssessmentRefusedError
    return value


def assess(
    *,
    root: str,
    run_id: str,
    receipt: str,
    output: str,
    original_candidate_sha256: str,
    evaluator_commit: str,
    evaluator_tree: str,
    evaluator_wheel_sha256: str,
    review_attestations_sha256: str,
) -> None:
    """Read, recompute both interpretations, reverify input, then write new output.

    Args:
        root: Authorised immutable evidence root, containing the run directory.
        run_id: Identifier used only for strict reader lookup; never emitted.
        receipt: Externally recorded manifest SHA-256 receipt.
        output: New assessment file outside the evidence root; never overwritten.
        original_candidate_sha256: Digest of the original tested candidate artifact.
        evaluator_commit: Corrected evaluator source commit assertion.
        evaluator_tree: Corrected evaluator source tree assertion.
        evaluator_wheel_sha256: Digest of the corrected evaluator artifact.
        review_attestations_sha256: Digest binding the independent review attestations.

    Raises:
        AssessmentRefusedError: On integrity, separation, evaluation or output refusal.
    """
    try:
        bindings = {
            "manifest_receipt_sha256": _digest(receipt),
            "original_candidate_sha256": _digest(original_candidate_sha256),
            "evaluator_commit": _digest(evaluator_commit, 40),
            "evaluator_tree": _digest(evaluator_tree, 40),
            "evaluator_wheel_sha256": _digest(evaluator_wheel_sha256),
            "review_attestations_sha256": _digest(review_attestations_sha256),
        }
        source = Path(root).resolve(strict=True)
        destination = Path(output)
        parent = destination.parent.resolve(strict=True)
        resolved = parent / destination.name
        if resolved.is_relative_to(source) or source.is_relative_to(resolved):
            raise AssessmentRefusedError
        retained = read_retained_run_v6(
            str(source), run_id=run_id, expected_manifest_sha256=receipt
        )
        baseline = check_current_conformance(retained)
        revised = check_revised_conformance(retained)
        # The reader re-verifies every manifested input byte, including the receipt.
        # This is not an atomic filesystem snapshot; authorised input is immutable.
        read_retained_run_v6(root, run_id=run_id, expected_manifest_sha256=receipt)
        document = {
            "assessment_schema_version": 1,
            "physical_qualification": "not_assessed",
            "input_integrity": "verified_against_external_receipt",
            "bindings": bindings,
            "original_policy4_baseline": baseline.model_dump(mode="json"),
            "revised_policy4": revised.model_dump(mode="json"),
        }
        payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # O_EXCL also refuses existing links, so no overwrite can touch sealed bytes.
        fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        raise AssessmentRefusedError from None


class _Parser(argparse.ArgumentParser):
    """Refuse malformed arguments without echoing potentially private values."""

    def error(self, message: str) -> NoReturn:
        raise AssessmentRefusedError


def main(argv: list[str] | None = None) -> int:
    """Run the offline assessor with fixed, sanitised status output.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero if written, one on refusal; never a physical qualification status.
    """
    parser = _Parser(description=__doc__)
    for name in (
        "root",
        "run-id",
        "receipt",
        "output",
        "original-candidate-sha256",
        "evaluator-commit",
        "evaluator-tree",
        "evaluator-wheel-sha256",
        "review-attestations-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    try:
        args = parser.parse_args(argv)
        assess(**vars(args))
    except AssessmentRefusedError:
        print("Offline assessment refused.")
        return 1
    print("Offline technical assessment written; physical qualification not assessed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
