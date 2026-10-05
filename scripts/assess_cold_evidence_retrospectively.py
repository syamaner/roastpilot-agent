"""Offline D211 technical assessment; never establishes physical qualification.

Use an external seal receipt, not a digest derived from the input manifest.
The output is separate from sealed evidence and contains closed outcomes and
content digests only. Supplied artifact/review digests are assertions, not proof
that a wheel was installed or that independent physical gates passed.
"""

import argparse
import json
import os
import secrets
from collections.abc import Generator
from contextlib import contextmanager
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


def _identity(fd: int) -> tuple[int, int]:
    """Return filesystem identity without following a pathname."""
    stat = os.fstat(fd)
    return stat.st_dev, stat.st_ino


@contextmanager
def _parent_descriptors(parent: Path, source: tuple[int, int]) -> Generator[list[int]]:
    """Hold every output ancestor, refusing links and evidence-root aliases."""
    descriptors: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(parent.anchor, flags))
        for component in parent.parts[1:]:
            if component == "..":
                raise AssessmentRefusedError
            descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
        if any(_identity(fd) == source for fd in descriptors):
            raise AssessmentRefusedError
        yield descriptors
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _recheck_parent(parent: Path, descriptors: list[int], source: tuple[int, int]) -> None:
    """Require every named ancestor still to match the held directory chain."""
    # Inspect actual ancestry before checking the named location, including a
    # directory moved into the evidence tree since the descriptors were opened.
    fd = os.dup(descriptors[-1])
    try:
        while True:
            identity = _identity(fd)
            if identity == source:
                raise AssessmentRefusedError
            ancestor = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            ancestor_identity = _identity(ancestor)
            os.close(fd)
            fd = ancestor
            if identity == ancestor_identity:
                break
    finally:
        os.close(fd)
    with _parent_descriptors(parent, source) as current:
        if [_identity(fd) for fd in current] != [_identity(fd) for fd in descriptors]:
            raise AssessmentRefusedError


def _publish(
    parent: Path, name: str, descriptors: list[int], source: tuple[int, int], payload: bytes
) -> None:
    """Stage privately and publish exclusively relative to the held parent."""
    parent_fd = descriptors[-1]
    temporary = ".cold-assessment-" + secrets.token_hex(16)
    staged = False
    published = False
    complete = False
    try:
        _recheck_parent(parent, descriptors, source)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        staged = True
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _recheck_parent(parent, descriptors, source)
        os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        published = True
        _recheck_parent(parent, descriptors, source)
        os.unlink(temporary, dir_fd=parent_fd)
        staged = False
        complete = True
    finally:
        if published and not complete:
            os.unlink(name, dir_fd=parent_fd)
        if staged:
            os.unlink(temporary, dir_fd=parent_fd)


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
        source_stat = source.stat()
        source_identity = (source_stat.st_dev, source_stat.st_ino)
        destination = Path(output).absolute()
        parent = destination.parent
        with _parent_descriptors(parent, source_identity) as descriptors:
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
            _publish(parent, destination.name, descriptors, source_identity, payload)
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
