"""Sanitised, allow-listed report over one interpreted cold-characterisation run.

The builder takes only a retained run, calls ``interpret_retained_run`` exactly
once, and projects the returned interpretation and its rebound capability into a
closed report schema.  Every string leaf is an enum member, a literal, or a
lowercase hexadecimal digest under an anchored pattern; the run and session
identifiers appear only as tagged SHA-256 digests.  Each report field is built
from one typed, allow-listed source: evidence is never dumped and then redacted.
The renderer builds that report once and emits canonical JSON bytes, re-parsed to
prove every object's key set, and a Markdown summary whose fixed templates walk
only the closed report model.

The module is pure.  It performs no file, network or process I/O and never calls
the retained-run reader, the evidence writer or the tree verifier.  It computes no
run verdict and compares no measurement with a limit: the locked D191 and
production limits are rendered, labelled not compared, and per-check results are
rendered as each check defines them.

Honest limit: the manifest digest is carried from the supplied run and is not
re-verified here, and nothing here establishes the run's completeness, provenance
or independent storage.  MCP-reported finalisation fields are recorded values, not
evaluated here.  Temperatures are Celsius only.
"""

import enum

__all__ = (
    "ColdReportError",
    "ColdReportFailure",
)


class ColdReportFailure(enum.Enum):
    """Closed failures for building or rendering a sanitised report."""

    REBIND_FAILED = "rebind_failed"
    VALUE_NOT_ADMITTED = "value_not_admitted"
    EGRESS_KEYSET_MISMATCH = "egress_keyset_mismatch"


class ColdReportError(RuntimeError):
    """Closed report error carrying only its failure member and a fixed message."""

    failure: ColdReportFailure

    def __init__(self, failure: ColdReportFailure) -> None:
        """Create a content-free report failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold report failed.")
        self.failure = failure
