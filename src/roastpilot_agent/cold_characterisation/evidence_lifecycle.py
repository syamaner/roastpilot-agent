"""Closed, versioned per-phase lifecycle evidence for cold characterisation (v2).

This module is pure: it defines the ``schema_version`` 2 lifecycle record written to
``records/<phase>/lifecycle.jsonl``, its lossless revalidation, and the run-wide
append-order state.  It performs no I/O, reads no clock, and decides nothing about
a run: over-budget or negative transitions, failed terminations, and missing
terminal records are recorded or returned as data for a later checker.  The v1
records, enums, and reader are unchanged; versioning is per stream.
"""

import enum
import math
import typing
from datetime import datetime, timedelta

import pydantic

from roastpilot_agent.cold_characterisation.duration_policy import admit_duration_generation
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_TEXT_FIELD_BYTES,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdPhaseKind,
    ColdRunHeader,
    walk_json_value,
)

#: Fixed AC20 bound on the phase transition; never configurable.
COLD_TRANSITION_BUDGET_SECONDS: typing.Final = 60.0

_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_DIGEST_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["identity_sha256"].metadata]
)


class ColdLifecycleEvent(enum.Enum):
    """Closed lifecycle events recorded per phase."""

    PHASE_ACTIVATED = "phase_activated"
    OBSERVATION_WINDOW_ELAPSED = "observation_window_elapsed"
    PHASE_ABORTED_NOT_FINALISED = "phase_aborted_not_finalised"
    FINALISATION_RETURNED = "finalisation_returned"
    CHILD_STOPPED = "child_stopped"
    CHILD_STARTED = "child_started"
    TRANSITION_MEASURED = "transition_measured"
    RUN_TERMINATED = "run_terminated"


class ColdLifecycleSessionAdmission(enum.Enum):
    """Whether a session identity was admitted into this record.

    ``NOT_ADMITTED`` means only that no session identity was admitted to evidence;
    it never proves absence of a session on the MCP side.
    """

    ADMITTED = "admitted"
    NOT_ADMITTED = "not_admitted"


class ColdLifecycleFinalisationResult(enum.Enum):
    """Closed classification of one finalisation return, as recorded."""

    CLEAN_RECORDED = "clean_recorded"
    NOT_CLEAN_RECORDED = "not_clean_recorded"
    FAILED_WITHOUT_RESULT = "failed_without_result"
    RECORD_NOT_RETAINED = "record_not_retained"


class ColdLifecycleChildStop(enum.Enum):
    """Whether an MCP child stop was confirmed."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class ColdLifecycleChildStart(enum.Enum):
    """Whether an MCP child start succeeded."""

    STARTED = "started"
    FAILED = "failed"


class ColdRunTermination(enum.Enum):
    """How the run ended, as recorded by its single terminal record."""

    COMPLETED = "completed"
    FAILED = "failed"


class ColdRunTerminationReason(enum.Enum):
    """Closed v2-owned reasons for a failed run; cancellation leaves no terminal."""

    PHASE_ADMISSION_REFUSED = "phase_admission_refused"
    PHASE_ABORTED = "phase_aborted"
    PHASE_FAILED_UNEXPECTEDLY = "phase_failed_unexpectedly"
    FINALISATION_FAILED = "finalisation_failed"
    FINALISATION_NOT_CLEAN = "finalisation_not_clean"
    FINALISATION_RECORD_NOT_RETAINED = "finalisation_record_not_retained"
    TEARDOWN_UNCONFIRMED = "teardown_unconfirmed"
    RESPAWN_FAILED = "respawn_failed"
    RECONNECT_FAILED = "reconnect_failed"
    RECORDING_ON_IDENTITY_REFUSED = "recording_on_identity_refused"
    PHASE_IDENTITY_DELTA_NOT_ADMITTED = "phase_identity_delta_not_admitted"
    TRANSITION_BUDGET_EXCEEDED = "transition_budget_exceeded"
    CLOCK_INVALID = "clock_invalid"
    CHILD_STOP_UNCONFIRMED = "child_stop_unconfirmed"
    UNEXPECTED_FAILURE = "unexpected_failure"


class ColdLifecycleFailure(enum.Enum):
    """Closed lifecycle ordering refusals."""

    SEQUENCE_NOT_CONTIGUOUS = "sequence_not_contiguous"
    PHASE_NOT_LATEST = "phase_not_latest"
    PHASE_REGRESSED = "phase_regressed"
    RECORDING_TIME_REGRESSED = "recording_time_regressed"
    APPENDED_AFTER_TERMINATION = "appended_after_termination"


class ColdLifecycleEvidenceState(enum.Enum):
    """Whether a retained run carries lifecycle evidence; ``ABSENT`` is never healthy."""

    ABSENT = "absent"
    PRESENT = "present"


class ColdLifecycleError(RuntimeError):
    """Closed lifecycle ordering error with a fixed message and no input content."""

    failure: ColdLifecycleFailure

    def __init__(self, failure: ColdLifecycleFailure) -> None:
        """Create a content-free lifecycle failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold lifecycle evidence refused.")
        self.failure = failure


def is_admissible_monotonic(value: object) -> bool:
    """Whether a value is an exact, finite, non-negative ``float``.

    Args:
        value: Candidate absolute monotonic instant.

    Returns:
        ``True`` only for an exact finite ``float`` of at least zero.
    """
    return type(value) is float and math.isfinite(value) and value >= 0.0


def _utf8_size(value: str) -> int | None:
    """Return a string's UTF-8 size, or ``None`` if it holds a surrogate."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def is_admissible_utc_instant(value: object) -> bool:
    """Whether a value is an exact bounded ISO-8601 string at UTC offset zero.

    Args:
        value: Candidate UTC instant.

    Returns:
        ``True`` only for an exact ``str`` of 1 to ``MAX_TEXT_FIELD_BYTES`` UTF-8
        bytes that ``datetime.fromisoformat`` parses with a zero UTC offset.
    """
    if type(value) is not str:
        return False
    size = _utf8_size(value)
    if size is None or not 1 <= size <= MAX_TEXT_FIELD_BYTES:
        return False
    try:
        offset = datetime.fromisoformat(value).utcoffset()
    except ValueError:
        return False
    return offset == timedelta(0)


def is_admissible_session_id(value: object) -> bool:
    """Whether a value is an exact, bounded, non-blank session identity.

    Args:
        value: Candidate MCP session identity.

    Returns:
        ``True`` only for an exact ``str`` of 1 to ``MAX_TEXT_FIELD_BYTES`` UTF-8
        bytes with no surrogate and a non-empty ``strip()``.
    """
    if type(value) is not str:
        return False
    size = _utf8_size(value)
    return size is not None and 1 <= size <= MAX_TEXT_FIELD_BYTES and bool(value.strip())


_OPTIONAL_FIELDS: tuple[str, ...] = (
    "session_admission",
    "session_id",
    "previous_phase_session_id",
    "scheduled_end_monotonic",
    "tick_count",
    "activation_deadline_exceeded",
    "finalisation_result",
    "child_stop",
    "child_start",
    "transition_start_monotonic",
    "transition_end_monotonic",
    "transition_seconds",
    "transition_budget_seconds",
    "transition_within_budget",
    "termination",
    "termination_reason",
)
_TRANSITION_FIELDS: tuple[str, ...] = (
    "transition_start_monotonic",
    "transition_end_monotonic",
    "transition_seconds",
    "transition_budget_seconds",
    "transition_within_budget",
)
_ADMISSION_EVENTS = frozenset(
    {
        ColdLifecycleEvent.PHASE_ACTIVATED,
        ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED,
        ColdLifecycleEvent.FINALISATION_RETURNED,
        ColdLifecycleEvent.TRANSITION_MEASURED,
    }
)
_REQUIRED_FIELDS: dict[ColdLifecycleEvent, frozenset[str]] = {
    ColdLifecycleEvent.PHASE_ACTIVATED: frozenset(
        {"session_admission", "session_id", "scheduled_end_monotonic"}
    ),
    ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED: frozenset(
        {"session_admission", "session_id", "scheduled_end_monotonic", "tick_count"}
    ),
    ColdLifecycleEvent.PHASE_ABORTED_NOT_FINALISED: frozenset(
        {"session_admission", "activation_deadline_exceeded"}
    ),
    ColdLifecycleEvent.FINALISATION_RETURNED: frozenset(
        {"session_admission", "session_id", "finalisation_result"}
    ),
    ColdLifecycleEvent.CHILD_STOPPED: frozenset({"child_stop"}),
    ColdLifecycleEvent.CHILD_STARTED: frozenset({"child_start"}),
    ColdLifecycleEvent.TRANSITION_MEASURED: frozenset(
        {"session_admission", "session_id", "previous_phase_session_id", *_TRANSITION_FIELDS}
    ),
    ColdLifecycleEvent.RUN_TERMINATED: frozenset({"termination"}),
}


class ColdLifecycleRecord(pydantic.BaseModel):
    """One flat, closed ``schema_version`` 2 lifecycle record.

    ``recorded_at_utc``/``monotonic_seconds`` are the append instant;
    ``event_utc``/``event_monotonic_seconds`` are when the event happened, which may
    precede the append but never follow it.  ``sequence`` orders lifecycle appends
    only; it is not proof of physical time order, and equal instants are valid.
    Session identities are private evidence recorded so a later checker can compare
    them; nothing here compares them.  Over-budget and negative transitions are
    recorded faithfully, never refused.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[2]
    stream: typing.Literal["lifecycle"]
    run_id: str
    phase: ColdPhaseKind
    recorded_at_utc: str
    monotonic_seconds: float
    identity_sha256: str
    sequence: int
    event: ColdLifecycleEvent
    event_utc: str
    event_monotonic_seconds: float
    session_admission: ColdLifecycleSessionAdmission | None
    session_id: str | None
    previous_phase_session_id: str | None
    scheduled_end_monotonic: float | None
    tick_count: int | None
    activation_deadline_exceeded: bool | None
    finalisation_result: ColdLifecycleFinalisationResult | None
    child_stop: ColdLifecycleChildStop | None
    child_start: ColdLifecycleChildStart | None
    transition_start_monotonic: float | None
    transition_end_monotonic: float | None
    transition_seconds: float | None
    transition_budget_seconds: float | None
    transition_within_budget: bool | None
    termination: ColdRunTermination | None
    termination_reason: ColdRunTerminationReason | None

    @pydantic.field_validator("schema_version", "sequence", "tick_count", mode="before")
    @classmethod
    def _require_exact_int(cls, value: object) -> object:
        """Refuse a ``bool``, ``float``, or any non-``int`` instead of coercing it."""
        if value is None or (type(value) is int and value >= 0):
            return value
        raise ValueError("value must be an exact non-negative int")

    @pydantic.field_validator("stream", "run_id", "identity_sha256", mode="before")
    @classmethod
    def _require_exact_text(cls, value: object) -> object:
        """Refuse a non-``str`` or ``str``-subclass value, then apply its pattern."""
        if type(value) is not str:
            raise ValueError("value must be an exact str")
        return value

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

    @pydantic.field_validator("recorded_at_utc", "event_utc", mode="before")
    @classmethod
    def _require_utc_instant(cls, value: object) -> object:
        """Refuse anything but an exact bounded UTC instant string."""
        if is_admissible_utc_instant(value):
            return value
        raise ValueError("value must be an exact UTC instant")

    @pydantic.field_validator(
        "monotonic_seconds",
        "event_monotonic_seconds",
        "scheduled_end_monotonic",
        "transition_start_monotonic",
        "transition_end_monotonic",
        mode="before",
    )
    @classmethod
    def _require_monotonic(cls, value: object) -> object:
        """Refuse anything but an exact finite non-negative ``float`` (or null)."""
        if value is None or is_admissible_monotonic(value):
            return value
        raise ValueError("value must be an exact non-negative finite float")

    @pydantic.field_validator("transition_seconds", mode="before")
    @classmethod
    def _require_signed_float(cls, value: object) -> object:
        """Refuse anything but an exact finite ``float`` (or null); the sign is kept."""
        if value is None or (type(value) is float and math.isfinite(value)):
            return value
        raise ValueError("value must be an exact finite float")

    @pydantic.field_validator("transition_budget_seconds", mode="before")
    @classmethod
    def _require_fixed_budget(cls, value: object) -> object:
        """Refuse anything but exactly the fixed ``float`` budget (or null)."""
        if value is None or (type(value) is float and value == COLD_TRANSITION_BUDGET_SECONDS):
            return value
        raise ValueError("value must be exactly the fixed transition budget")

    @pydantic.field_validator("session_id", "previous_phase_session_id", mode="before")
    @classmethod
    def _require_session_id(cls, value: object) -> object:
        """Refuse anything but an exact bounded non-blank session identity (or null)."""
        if value is None or is_admissible_session_id(value):
            return value
        raise ValueError("value must be an admissible session id")

    @pydantic.field_validator(
        "activation_deadline_exceeded", "transition_within_budget", mode="before"
    )
    @classmethod
    def _require_exact_bool(cls, value: object) -> object:
        """Refuse anything but an exact ``bool`` (or null)."""
        if value is None or type(value) is bool:
            return value
        raise ValueError("value must be an exact bool")

    @pydantic.model_validator(mode="after")
    def _require_event_matrix(self) -> typing.Self:
        """Apply the closed per-event field matrix and derived-field exactness."""
        if self.event_monotonic_seconds > self.monotonic_seconds:
            raise ValueError("event instant follows the recording instant")
        required = set(_REQUIRED_FIELDS[self.event])
        if self.session_admission is ColdLifecycleSessionAdmission.ADMITTED:
            required.add("session_id")
        if self.termination is ColdRunTermination.FAILED:
            required.add("termination_reason")
        for name in _OPTIONAL_FIELDS:
            if (getattr(self, name) is not None) != (name in required):
                raise ValueError("field presence breaks the event matrix")
        if (
            self.event in _ADMISSION_EVENTS
            and self.session_admission is not ColdLifecycleSessionAdmission.ADMITTED
        ):
            raise ValueError("event requires an admitted session")
        event_instant = self.event_monotonic_seconds
        scheduled_end = typing.cast(float, self.scheduled_end_monotonic)
        if self.event is ColdLifecycleEvent.PHASE_ACTIVATED:
            if admit_duration_generation(event_instant, scheduled_end) is None:
                raise ValueError("scheduled end is not activation plus the phase length")
        elif self.event is ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED:
            if event_instant < scheduled_end:
                raise ValueError("observation window elapsed before its scheduled end")
        elif self.event is ColdLifecycleEvent.TRANSITION_MEASURED:
            start = typing.cast(float, self.transition_start_monotonic)
            seconds = typing.cast(float, self.transition_seconds)
            if (
                self.phase is not ColdPhaseKind.RECORDING_ON
                or self.transition_end_monotonic != event_instant
                or seconds != event_instant - start
                or self.transition_budget_seconds != COLD_TRANSITION_BUDGET_SECONDS
                or self.transition_within_budget
                is not (0.0 <= seconds <= COLD_TRANSITION_BUDGET_SECONDS)
            ):
                raise ValueError("transition fields are not exactly derived")
        return self


def _grammar_admits(adapter: pydantic.TypeAdapter[str], value: str) -> bool:
    """Whether one exact string satisfies a delivered v1 header grammar."""
    try:
        adapter.validate_python(value, strict=True)
    except pydantic.ValidationError:
        return False
    return True


_FIELD_NAMES: frozenset[str] = frozenset(ColdLifecycleRecord.model_fields)
_ADMITTED_ENUM_TYPES: tuple[type[enum.Enum], ...] = (
    ColdPhaseKind,
    ColdLifecycleEvent,
    ColdLifecycleSessionAdmission,
    ColdLifecycleFinalisationResult,
    ColdLifecycleChildStop,
    ColdLifecycleChildStart,
    ColdRunTermination,
    ColdRunTerminationReason,
    ColdLifecycleFailure,
    ColdLifecycleEvidenceState,
)
ADMITTED_ENUM_TYPES: typing.Final = _ADMITTED_ENUM_TYPES
_SCALAR_TYPES: tuple[type[object], ...] = (bool, int, float, str)


def _raw_fields(record: ColdLifecycleRecord) -> dict[str, object]:
    """Return a forged-proof copy of exactly the declared raw field values."""
    data = object.__getattribute__(record, "__dict__")
    extra = object.__getattribute__(record, "__pydantic_extra__")
    if type(data) is not dict or extra is not None and (type(extra) is not dict or extra):
        raise ValueError("record carries undeclared state")
    raw = typing.cast(dict[object, object], data)
    if len(raw) != len(_FIELD_NAMES) or any(name not in raw for name in _FIELD_NAMES):
        raise ValueError("record field set is not exact")
    values: dict[str, object] = {}
    for name in ColdLifecycleRecord.model_fields:
        value = raw[name]
        if not (
            value is None or type(value) in _SCALAR_TYPES or type(value) in _ADMITTED_ENUM_TYPES
        ):
            raise ValueError("record value type is not admitted")
        values[name] = value
    return values


def validate_lifecycle_record(record: ColdLifecycleRecord) -> ColdLifecycleRecord:
    """Losslessly revalidate one lifecycle record and return a fresh snapshot.

    Forged ``model_construct``/``model_copy`` instances, undeclared or missing
    state, and non-exact values are all refused.

    Args:
        record: One exact in-process lifecycle record.

    Returns:
        A newly validated snapshot.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, or the shared walker's own
            member for a JSON bound breach.
    """
    try:
        if type(record) is not ColdLifecycleRecord:
            raise ValueError("record class is not exact")
        values = _raw_fields(record)
        view = {
            name: value.value if isinstance(value, enum.Enum) else value
            for name, value in values.items()
        }
        walk_json_value(typing.cast(pydantic.JsonValue, view))
        validated = ColdLifecycleRecord.model_validate(values, strict=True)
    except ColdEvidenceError as error:
        failure = error
    except Exception:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validated
    raise failure


_PHASE_ORDER: tuple[ColdPhaseKind, ...] = tuple(ColdPhaseKind)


class ColdLifecycleSequence:
    """Pure run-wide lifecycle append-order state shared by writer and reader."""

    __slots__ = ("_last_phase", "_last_recorded", "_next_sequence", "_terminated")

    def __init__(self) -> None:
        """Create empty ordering state."""
        self._next_sequence = 0
        self._last_phase: ColdPhaseKind | None = None
        self._last_recorded: float | None = None
        self._terminated = False

    @property
    def next_sequence(self) -> int:
        """The only ``sequence`` the next record may carry."""
        return self._next_sequence

    @property
    def terminated(self) -> bool:
        """Whether a ``RUN_TERMINATED`` record has been committed."""
        return self._terminated

    def check(self, record: ColdLifecycleRecord) -> None:
        """Refuse a record that would break the run-wide order; changes nothing.

        Args:
            record: A validated lifecycle snapshot.

        Raises:
            ColdLifecycleError: If the record follows termination, breaks contiguity,
                regresses the phase, or regresses the recording instant.
        """
        if self._terminated:
            raise ColdLifecycleError(ColdLifecycleFailure.APPENDED_AFTER_TERMINATION)
        if record.sequence != self._next_sequence:
            raise ColdLifecycleError(ColdLifecycleFailure.SEQUENCE_NOT_CONTIGUOUS)
        if self._last_phase is not None and _PHASE_ORDER.index(record.phase) < _PHASE_ORDER.index(
            self._last_phase
        ):
            raise ColdLifecycleError(ColdLifecycleFailure.PHASE_REGRESSED)
        if self._last_recorded is not None and record.monotonic_seconds < self._last_recorded:
            raise ColdLifecycleError(ColdLifecycleFailure.RECORDING_TIME_REGRESSED)

    def commit(self, record: ColdLifecycleRecord) -> None:
        """Advance the state past one checked record.

        Args:
            record: The record most recently accepted by :meth:`check`.
        """
        self._next_sequence = record.sequence + 1
        self._last_phase = record.phase
        self._last_recorded = record.monotonic_seconds
        if record.event is ColdLifecycleEvent.RUN_TERMINATED:
            self._terminated = True
