"""Closed, versioned failed-run terminal evidence for cold characterisation.

This module is pure: it defines the per-stream ``schema_version`` 3 record written to
``records/<phase>/failed_run_terminal.jsonl``, its content re-admission, one
header-bound builder, and a pure order check shared by the writer and the reader.
It performs no I/O, reads no clock, calls no provider, and decides nothing about a
run.  The record carries observations only: identity values, closed members, and
non-negative counts.  It has no field for time, sealing, a stop, a child, a
session, safety, or free text, and it never qualifies a run.  The closed version-2
lifecycle termination record and its reason grammar are unchanged; an MCP child
stop stays in the v2 lifecycle ``CHILD_STOPPED`` grammar, which this record's
lifecycle count binds.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdPhaseKind,
    ColdRunHeader,
    validate_record,
    walk_json_value,
)

_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_DIGEST_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["identity_sha256"].metadata]
)


class ColdFailedRunAdvisorySettlement(enum.Enum):
    """The stored phase-end advisory settlement observation, recorded as data.

    Values equal the stored sampler settlement members that describe a call still
    unresolved or unrecorded at phase end; a contract test pins that parity.  A
    member is an observation, never a runtime decision.  ``RECORDED_UNRESOLVED_NOT_INVOKED``
    means only that the sampler did not know of an invocation: it does not prove
    that no provider side effect, task, or remote call exists.  No member means
    success or health.
    """

    RECORDED_UNRESOLVED_INVOKED = "recorded_unresolved_invoked"
    RECORDED_UNRESOLVED_NOT_INVOKED = "recorded_unresolved_not_invoked"
    NOT_RECORDED_CLOCK_INVALID = "not_recorded_clock_invalid"
    NOT_RECORDED_SINK_REFUSED = "not_recorded_sink_refused"
    NOT_RECORDED_COMPLETION_UNKNOWN = "not_recorded_completion_unknown"


class ColdFailedRunProviderCancellation(enum.Enum):
    """The stored settlement-time provider cancellation request, recorded as data.

    Values equal the stored sampler cancellation-request members; a contract test
    pins that parity.  ``REQUESTED`` means only that ``Task.cancel`` accepted the
    request: it proves neither delivery nor that the provider stopped, so the
    cancellation stays requested and unconfirmed.  ``NO_PROVIDER_TASK`` means no
    published provider task, never proof that no task or remote call exists.
    """

    NO_PROVIDER_TASK = "no_provider_task"
    TASK_ALREADY_DONE = "task_already_done"
    REQUESTED = "requested"
    REQUEST_NOT_ACCEPTED = "request_not_accepted"
    REQUEST_RAISED = "request_raised"
    REQUEST_INTERRUPTED = "request_interrupted"


class ColdFailedRunTerminalFailure(enum.Enum):
    """Closed failed-run terminal ordering refusals."""

    ALREADY_TERMINATED = "already_terminated"
    PHASE_NOT_LATEST = "phase_not_latest"
    LIFECYCLE_COUNT_MISMATCHED = "lifecycle_count_mismatched"
    ADVISORY_COUNT_MISMATCHED = "advisory_count_mismatched"
    APPENDED_AFTER_TERMINAL = "appended_after_terminal"
    TERMINAL_DUPLICATED = "terminal_duplicated"


class ColdFailedRunTerminalEvidenceState(enum.Enum):
    """Whether a retained run carries a failed-run terminal; ``ABSENT`` is never healthy."""

    ABSENT = "absent"
    PRESENT = "present"


class ColdFailedRunTerminalError(Exception):
    """Closed failed-run terminal error with a fixed message and no input content."""

    failure: ColdFailedRunTerminalFailure

    def __init__(self, failure: ColdFailedRunTerminalFailure) -> None:
        """Create a content-free failed-run terminal failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold failed-run terminal evidence refused.")
        self.failure = failure


def _grammar_admits(adapter: pydantic.TypeAdapter[str], value: str) -> bool:
    """Whether one exact string satisfies a delivered v1 header grammar."""
    try:
        adapter.validate_python(value, strict=True)
    except pydantic.ValidationError:
        return False
    return True


class ColdFailedRunTerminalRecord(pydantic.BaseModel):
    """One flat, closed ``schema_version`` 3 failed-run terminal record.

    It records that a run ended as a failed run, the stored advisory settlement
    and provider cancellation observations, and how many lifecycle and advisory
    attempt lines were retained before it.  It cannot assert that its own seal
    succeeded, that a provider, task, or child stopped, that no session exists, or
    that the hardware is safe.  It never qualifies a run.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[3]
    stream: typing.Literal["failed_run_terminal"]
    run_id: str
    identity_sha256: str
    phase: ColdPhaseKind
    advisory_settlement: ColdFailedRunAdvisorySettlement
    provider_cancellation: ColdFailedRunProviderCancellation
    lifecycle_records_retained: int
    advisory_attempt_records_retained: int

    @pydantic.field_validator(
        "schema_version",
        "lifecycle_records_retained",
        "advisory_attempt_records_retained",
        mode="before",
    )
    @classmethod
    def _require_count(cls, value: object) -> object:
        """Refuse a ``bool``, ``float``, negative, or non-``int`` instead of coercing it."""
        if type(value) is int and value >= 0:
            return value
        raise ValueError("value must be an exact non-negative int")

    @pydantic.field_validator("stream", "run_id", "identity_sha256", mode="before")
    @classmethod
    def _require_exact_text(cls, value: object) -> object:
        """Refuse a non-``str`` or ``str``-subclass value."""
        if type(value) is str:
            return value
        raise ValueError("value must be an exact str")

    @pydantic.field_validator("run_id", mode="after")
    @classmethod
    def _require_run_id(cls, value: str) -> str:
        """Apply the v1 header run-id grammar."""
        if _grammar_admits(_RUN_ID_ADAPTER, value):
            return value
        raise ValueError("run id grammar")

    @pydantic.field_validator("identity_sha256", mode="after")
    @classmethod
    def _require_digest(cls, value: str) -> str:
        """Apply the v1 header identity-digest grammar."""
        if _grammar_admits(_DIGEST_ADAPTER, value):
            return value
        raise ValueError("digest grammar")


_SCALAR_TYPES: tuple[type[object], ...] = (int, str)
_ADMITTED_ENUM_TYPES: tuple[type[enum.Enum], ...] = (
    ColdPhaseKind,
    ColdFailedRunAdvisorySettlement,
    ColdFailedRunProviderCancellation,
)


def _is_admitted_member(value: object) -> bool:
    """Whether a value is a real member of an admitted enum, found by identity."""
    for admitted in _ADMITTED_ENUM_TYPES:
        if type(value) is admitted:
            return any(value is member for member in admitted)
    return False


def _raw_fields(record: ColdFailedRunTerminalRecord) -> dict[str, object]:
    """Return a copy of exactly the declared raw field values.

    Raises:
        ValueError: If undeclared or missing state, a non-``str`` key, or a value
            that is not an exact ``int``, an exact ``str``, or a real admitted
            member appears.
    """
    data = object.__getattribute__(record, "__dict__")
    extra = object.__getattribute__(record, "__pydantic_extra__")
    if type(data) is not dict or extra is not None and (type(extra) is not dict or extra):
        raise ValueError("record carries undeclared state")
    raw = typing.cast(dict[object, object], data)
    if any(type(name) is not str for name in raw) or set(raw) != set(
        ColdFailedRunTerminalRecord.model_fields
    ):
        raise ValueError("record field set is not exact")
    values: dict[str, object] = {}
    for name, value in raw.items():
        if not (type(value) in _SCALAR_TYPES or _is_admitted_member(value)):
            raise ValueError("record value type is not admitted")
        values[typing.cast(str, name)] = value
    return values


def validate_failed_run_terminal_record(
    record: ColdFailedRunTerminalRecord,
) -> ColdFailedRunTerminalRecord:
    """Re-admit one record's content and return a freshly validated snapshot.

    Subclasses, undeclared or missing state, foreign or fabricated enum members,
    and non-exact values are refused.  The exact admitted values are walked by the
    shared JSON walker (members projected to their values) before strict model
    validation, so a count outside the shared integer-digit bound is refused.

    Honest limits: this re-admits content; it cannot prove provenance.  An exact
    class instance built by ``model_construct`` or ``model_copy`` whose content
    would validate is admitted, because it is the same as validated content, and
    the returned snapshot is freshly validated.

    Args:
        record: One exact in-process failed-run terminal record.

    Returns:
        A newly validated snapshot.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, or the shared walker's own
            member for a JSON bound breach.
    """
    try:
        if type(record) is not ColdFailedRunTerminalRecord:
            raise ValueError("record class is not exact")
        values = _raw_fields(record)
        view = {
            name: value.value if isinstance(value, enum.Enum) else value
            for name, value in values.items()
        }
        walk_json_value(typing.cast(pydantic.JsonValue, view))
        validated = ColdFailedRunTerminalRecord.model_validate(values, strict=True)
    except ColdEvidenceError as error:
        failure = error
    except Exception:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validated
    raise failure


def build_failed_run_terminal_record(
    header: ColdRunHeader,
    *,
    advisory_settlement: ColdFailedRunAdvisorySettlement,
    provider_cancellation: ColdFailedRunProviderCancellation,
    lifecycle_records_retained: int,
    advisory_attempt_records_retained: int,
) -> ColdFailedRunTerminalRecord:
    """Build one header-bound failed-run terminal record.

    Only the header's run, phase, and identity digest are used.  The version and
    stream are fixed; strict model validation is the single guard for the four
    arguments, which are recorded as supplied.

    Args:
        header: The exact bound phase header.
        advisory_settlement: The stored advisory settlement observation.
        provider_cancellation: The stored provider cancellation observation.
        lifecycle_records_retained: Lifecycle lines retained before this record.
        advisory_attempt_records_retained: Advisory attempt lines retained before it.

    Returns:
        A validated record snapshot.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, or another closed member from
            header revalidation or the shared walker.
    """
    snapshot = validate_record(header)
    if type(snapshot) is not ColdRunHeader:
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    try:
        record = ColdFailedRunTerminalRecord.model_validate(
            {
                "schema_version": 3,
                "stream": "failed_run_terminal",
                "run_id": snapshot.run_id,
                "identity_sha256": snapshot.identity_sha256,
                "phase": snapshot.phase,
                "advisory_settlement": advisory_settlement,
                "provider_cancellation": provider_cancellation,
                "lifecycle_records_retained": lifecycle_records_retained,
                "advisory_attempt_records_retained": advisory_attempt_records_retained,
            },
            strict=True,
        )
    except pydantic.ValidationError:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validate_failed_run_terminal_record(record)
    raise failure


def check_failed_run_terminal_order(
    record: ColdFailedRunTerminalRecord,
    *,
    latest_phase: ColdPhaseKind,
    lifecycle_terminated: bool,
    lifecycle_records: int,
    advisory_records: int,
) -> None:
    """Refuse a terminal that contradicts the retained run; changes nothing.

    The first matching refusal wins: a committed v2 run termination, then a phase
    other than the latest bound phase, then a lifecycle count, then an advisory
    attempt count that differs from the retained lines.

    Args:
        record: A validated failed-run terminal snapshot.
        latest_phase: The latest bound phase.
        lifecycle_terminated: Whether a v2 ``RUN_TERMINATED`` record was committed.
        lifecycle_records: Lifecycle lines retained in the run.
        advisory_records: Advisory attempt lines retained in the run.

    Raises:
        ColdFailedRunTerminalError: With the first closed refusal that applies.
    """
    if lifecycle_terminated:
        raise ColdFailedRunTerminalError(ColdFailedRunTerminalFailure.ALREADY_TERMINATED)
    if record.phase is not latest_phase:
        raise ColdFailedRunTerminalError(ColdFailedRunTerminalFailure.PHASE_NOT_LATEST)
    if record.lifecycle_records_retained != lifecycle_records:
        raise ColdFailedRunTerminalError(ColdFailedRunTerminalFailure.LIFECYCLE_COUNT_MISMATCHED)
    if record.advisory_attempt_records_retained != advisory_records:
        raise ColdFailedRunTerminalError(ColdFailedRunTerminalFailure.ADVISORY_COUNT_MISMATCHED)
