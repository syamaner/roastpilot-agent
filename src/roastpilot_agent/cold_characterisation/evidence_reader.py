"""Versioned strict reader for verified retained cold-characterisation evidence.

Unverified bytes are never parsed: the tree is verified against its expected
manifest digest first, and every record file is re-read under identity checks
and required to match its manifest digest before any line is decoded.  The
reader returns integrity-checked records only; it computes no outcome.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_advisory import (
    ColdAdvisoryAttemptEvidenceState,
    ColdAdvisoryAttemptRecord,
    ColdAdvisoryEntry,
    ColdAdvisoryIntentRecord,
    ColdAdvisoryResolutionRecord,
    ColdAdvisorySequence,
    validate_advisory_attempt_record,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    ColdLifecycleEvidenceState,
    ColdLifecycleRecord,
    ColdLifecycleSequence,
    validate_lifecycle_record,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_RECORD_BYTES,
    ColdAbortDomain,
    ColdAbortRecord,
    ColdAdvisorFailureKind,
    ColdAdvisoryRecord,
    ColdEngineAbortReason,
    ColdEvidenceFailure,
    ColdEvidenceRecord,
    ColdEvidenceStream,
    ColdFinalisationRecord,
    ColdHostAbortReason,
    ColdHostRecord,
    ColdIdentityAbortReason,
    ColdMcpAbortReason,
    ColdOperatorAbortReason,
    ColdPhaseKind,
    ColdRunHeader,
    ColdTickRecord,
    validate_record,
    walk_json_value,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdBindingState,
    ColdEvidenceStoreError,
    ColdEvidenceStoreFailure,
    ColdRetainedIdentityV1,
    ColdVerifiedTree,
    canonical_json,
    check_advisory_attempt_binding,
    check_failed_run_terminal_binding,
    check_lifecycle_binding,
    check_record_binding,
    check_temperature_run_binding,
    check_tick_temperature_binding,
    load_strict_json,
    read_verified_lines,
    run_id_is_valid,
    verify_retained_tree,
)
from roastpilot_agent.cold_characterisation.evidence_temperature import (
    TICK_TEMPERATURE_FILE_NAME,
    TICK_TEMPERATURE_STREAM,
    ColdTickTemperatureError,
    ColdTickTemperatureEvidenceState,
    ColdTickTemperatureFailure,
    ColdTickTemperatureRecord,
    check_tick_temperature_pairing,
    decode_tick_temperature_document,
    validate_tick_temperature_record,
)
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    MCP_CANDIDATE_FILE_NAME,
    MCP_CANDIDATE_STREAM,
    TEMPERATURE_ABORT_FILE_NAME,
    TEMPERATURE_ABORT_STREAM,
    ColdMcpCandidateRecord,
    ColdTemperatureAbortRecord,
    ColdTemperatureRunError,
    ColdTemperatureRunFailure,
    ColdTemperatureScreenReason,
    decode_mcp_candidate_document,
    decode_temperature_abort_document,
    validate_mcp_candidate_record,
    validate_temperature_abort_record,
)
from roastpilot_agent.cold_characterisation.evidence_terminal import (
    ColdFailedRunTerminalError,
    ColdFailedRunTerminalEvidenceState,
    ColdFailedRunTerminalFailure,
    ColdFailedRunTerminalRecord,
    check_failed_run_terminal_order,
    validate_failed_run_terminal_record,
)

MAX_LINE_BYTES = MAX_RECORD_BYTES + 1
_RECORDS_DIRECTORY = "records"
_SCHEMA_VERSIONS = frozenset({1})
_PHASES_BY_NAME = {phase.value: phase for phase in ColdPhaseKind}
_STREAMS_BY_FILE_NAME = {f"{stream.value}.jsonl": stream for stream in ColdEvidenceStream}
_STREAMS_BY_VALUE = {stream.value: stream for stream in ColdEvidenceStream}
_LIFECYCLE_FILE_NAME = "lifecycle.jsonl"
_LIFECYCLE_STREAM = "lifecycle"
_LIFECYCLE_SCHEMA_VERSIONS = frozenset({2})
_ADVISORY_FILE_NAME = "advisory_attempt.jsonl"
_ADVISORY_STREAM = "advisory_attempt"
_ADVISORY_SCHEMA_VERSIONS = frozenset({2})
_TERMINAL_FILE_NAME = "failed_run_terminal.jsonl"
_TERMINAL_STREAM = "failed_run_terminal"
_TERMINAL_SCHEMA_VERSIONS = frozenset({3})
_TICK_TEMPERATURE_FILE_NAME = TICK_TEMPERATURE_FILE_NAME
_TICK_TEMPERATURE_STREAM = TICK_TEMPERATURE_STREAM
_TICK_TEMPERATURE_SCHEMA_VERSIONS = frozenset({4})
_TEMPERATURE_ABORT_FILE_NAME = TEMPERATURE_ABORT_FILE_NAME
_TEMPERATURE_ABORT_STREAM = TEMPERATURE_ABORT_STREAM
_TEMPERATURE_ABORT_SCHEMA_VERSIONS = frozenset({5})
_MCP_CANDIDATE_FILE_NAME = MCP_CANDIDATE_FILE_NAME
_MCP_CANDIDATE_STREAM = MCP_CANDIDATE_STREAM
_MCP_CANDIDATE_SCHEMA_VERSIONS = frozenset({6})


class _ReadProfile(enum.Enum):
    """Closed reader profiles; each value is the immutable set of extra file names admitted.

    A profile is a reader grammar, not a record schema version.
    """

    V1 = frozenset[str]()
    V2 = frozenset({_LIFECYCLE_FILE_NAME})
    V3 = frozenset({_LIFECYCLE_FILE_NAME, _ADVISORY_FILE_NAME})
    V4 = frozenset({_LIFECYCLE_FILE_NAME, _ADVISORY_FILE_NAME, _TERMINAL_FILE_NAME})
    V5 = frozenset(
        {
            _LIFECYCLE_FILE_NAME,
            _ADVISORY_FILE_NAME,
            _TERMINAL_FILE_NAME,
            _TICK_TEMPERATURE_FILE_NAME,
        }
    )
    V6 = frozenset(
        {
            _LIFECYCLE_FILE_NAME,
            _ADVISORY_FILE_NAME,
            _TERMINAL_FILE_NAME,
            _TICK_TEMPERATURE_FILE_NAME,
            _TEMPERATURE_ABORT_FILE_NAME,
            _MCP_CANDIDATE_FILE_NAME,
        }
    )


#: Reader-local abort pairing; a contract test pins it to ``ColdAbortRecord``'s validator.
ABORT_REASON_BY_DOMAIN: dict[ColdAbortDomain, type[enum.Enum]] = {
    ColdAbortDomain.HOST: ColdHostAbortReason,
    ColdAbortDomain.IDENTITY: ColdIdentityAbortReason,
    ColdAbortDomain.EVIDENCE: ColdEvidenceFailure,
    ColdAbortDomain.MCP: ColdMcpAbortReason,
    ColdAbortDomain.ADVISOR: ColdAdvisorFailureKind,
    ColdAbortDomain.OPERATOR: ColdOperatorAbortReason,
    ColdAbortDomain.ENGINE: ColdEngineAbortReason,
}

_HEADER_ADAPTER = pydantic.TypeAdapter(ColdRunHeader)
_TICK_ADAPTER = pydantic.TypeAdapter(ColdTickRecord)
_HOST_ADAPTER = pydantic.TypeAdapter(ColdHostRecord)
_ADVISORY_ADAPTER = pydantic.TypeAdapter(ColdAdvisoryRecord)
_FINALISATION_ADAPTER = pydantic.TypeAdapter(ColdFinalisationRecord)
_ABORT_ADAPTER = pydantic.TypeAdapter(ColdAbortRecord)
_LIFECYCLE_ADAPTER = pydantic.TypeAdapter(ColdLifecycleRecord)
_ADVISORY_ADAPTERS: dict[str, pydantic.TypeAdapter[ColdAdvisoryAttemptRecord]] = {
    ColdAdvisoryEntry.INTENT.value: pydantic.TypeAdapter(ColdAdvisoryIntentRecord),
    ColdAdvisoryEntry.RESOLUTION.value: pydantic.TypeAdapter(ColdAdvisoryResolutionRecord),
}
_TERMINAL_ADAPTER = pydantic.TypeAdapter(ColdFailedRunTerminalRecord)


class ColdRetainedHeader(pydantic.BaseModel):
    """One phase header with the v1 identity it retains."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    header: ColdRunHeader
    identity: ColdRetainedIdentityV1


class ColdRetainedStream(pydantic.BaseModel):
    """One phase stream's records in file order."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    phase: ColdPhaseKind
    stream: ColdEvidenceStream
    records: tuple[ColdEvidenceRecord, ...]


class ColdRetainedRun(pydantic.BaseModel):
    """A verified, strictly read retained run; integrity facts only, no outcome."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run_id: str
    manifest_sha256: str
    headers: tuple[ColdRetainedHeader, ...]
    streams: tuple[ColdRetainedStream, ...]


class ColdRetainedRunV2(pydantic.BaseModel):
    """A verified retained run plus its v2 lifecycle records; integrity facts only.

    ``ABSENT`` means the tree carries no lifecycle stream (for example a v1 tree); it
    is never healthy.  A missing terminal record is returned as data.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run: ColdRetainedRun
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[ColdLifecycleRecord, ...]

    @pydantic.model_validator(mode="after")
    def _require_state_matches_records(self) -> typing.Self:
        """Require ``ABSENT`` exactly when no lifecycle record is present."""
        if (self.lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (self.lifecycle == ()):
            raise ValueError("lifecycle state does not match its records")
        return self


class ColdRetainedRunV3(pydantic.BaseModel):
    """A verified retained run with its lifecycle and advisory-attempt streams.

    Integrity facts only.  ``ABSENT`` and ``OPEN_TAIL`` are never healthy, and
    ``COMPLETE`` means only that every retained attempt is structurally resolved,
    possibly as failed, abandoned, or unresolved at phase end.  It is the input to
    advisory conformance policy 2 (``check_advisory_conformance``); a hand-built
    instance carries no manifest provenance.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run: ColdRetainedRun
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[ColdLifecycleRecord, ...]
    advisory_attempt_state: ColdAdvisoryAttemptEvidenceState
    advisory_attempts: tuple[ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord, ...]

    @pydantic.model_validator(mode="after")
    def _require_states_match_records(self) -> typing.Self:
        """Require each stream state to be exactly the one its records imply."""
        if (self.lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (self.lifecycle == ()):
            raise ValueError("lifecycle state does not match its records")
        if self.advisory_attempt_state is not _advisory_state(self.advisory_attempts):
            raise ValueError("advisory attempt state does not match its records")
        return self


class ColdRetainedRunV4(pydantic.BaseModel):
    """A verified retained run with lifecycle, advisory attempts, and a failed-run terminal.

    Integrity facts only.  A failed-run terminal is integrity data: it never
    qualifies a run, and ``ABSENT`` is never healthy.  This carrier is flat; it is
    not a v2 or v3 carrier, nests none, and is the input to no conformance policy.
    A hand-built instance carries no manifest provenance.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run: ColdRetainedRun
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[ColdLifecycleRecord, ...]
    advisory_attempt_state: ColdAdvisoryAttemptEvidenceState
    advisory_attempts: tuple[ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord, ...]
    terminal_state: ColdFailedRunTerminalEvidenceState
    terminal: ColdFailedRunTerminalRecord | None

    @pydantic.model_validator(mode="after")
    def _require_states_match_records(self) -> typing.Self:
        """Require each stream state to be exactly the one its records imply."""
        if (self.lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (self.lifecycle == ()):
            raise ValueError("lifecycle state does not match its records")
        if self.advisory_attempt_state is not _advisory_state(self.advisory_attempts):
            raise ValueError("advisory attempt state does not match its records")
        if (self.terminal_state is ColdFailedRunTerminalEvidenceState.ABSENT) != (
            self.terminal is None
        ):
            raise ValueError("terminal state does not match its record")
        return self


class ColdRetainedRunV5(pydantic.BaseModel):
    """A verified retained run with V4's streams plus positionally paired tick temperatures.

    Integrity data only: this carrier is flat, nests no earlier carrier, and is the
    input to no policy.  ``PRESENT`` means only that every retained v1 tick pairs
    one-to-one, in order, with a tick-temperature record; it is neither screening
    nor provenance.  ``ABSENT`` neither proves historical origin nor satisfies any
    screening: a stripped and re-sealed tree also reads ``ABSENT``.  A hand-built
    or ``model_construct`` instance carries no manifest provenance, so a later
    consumer must check the exact type and re-admit every record.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run: ColdRetainedRun
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[ColdLifecycleRecord, ...]
    advisory_attempt_state: ColdAdvisoryAttemptEvidenceState
    advisory_attempts: tuple[ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord, ...]
    terminal_state: ColdFailedRunTerminalEvidenceState
    terminal: ColdFailedRunTerminalRecord | None
    tick_temperature_state: ColdTickTemperatureEvidenceState
    tick_temperatures: tuple[ColdTickTemperatureRecord, ...]

    @pydantic.model_validator(mode="after")
    def _require_states_match_records(self) -> typing.Self:
        """Require each stream state to be exactly the one its records imply."""
        if (self.lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (self.lifecycle == ()):
            raise ValueError("lifecycle state does not match its records")
        if self.advisory_attempt_state is not _advisory_state(self.advisory_attempts):
            raise ValueError("advisory attempt state does not match its records")
        if (self.terminal_state is ColdFailedRunTerminalEvidenceState.ABSENT) != (
            self.terminal is None
        ):
            raise ValueError("terminal state does not match its record")
        if (self.tick_temperature_state is ColdTickTemperatureEvidenceState.ABSENT) != (
            self.tick_temperatures == ()
        ):
            raise ValueError("tick temperature state does not match its records")
        return self


class ColdRetainedRunV6(pydantic.BaseModel):
    """A verified retained run with V5's streams plus D209 temperature aborts and candidates.

    Integrity data only: this carrier is flat, nests no earlier carrier, and is the
    sole input to temperature conformance policy 3.  A retained temperature abort
    requires ``PRESENT`` tick temperatures.  A candidate record is an operator
    assertion, never attestation of authenticity or installed bytes.  ``ABSENT``
    neither proves historical origin nor satisfies any screening.  A hand-built or
    ``model_construct`` instance carries no manifest provenance, so a consumer must
    check the exact type and re-admit every record.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run: ColdRetainedRun
    lifecycle_state: ColdLifecycleEvidenceState
    lifecycle: tuple[ColdLifecycleRecord, ...]
    advisory_attempt_state: ColdAdvisoryAttemptEvidenceState
    advisory_attempts: tuple[ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord, ...]
    terminal_state: ColdFailedRunTerminalEvidenceState
    terminal: ColdFailedRunTerminalRecord | None
    tick_temperature_state: ColdTickTemperatureEvidenceState
    tick_temperatures: tuple[ColdTickTemperatureRecord, ...]
    temperature_aborts: tuple[ColdTemperatureAbortRecord, ...]
    mcp_candidates: tuple[ColdMcpCandidateRecord, ...]

    @pydantic.model_validator(mode="after")
    def _require_states_match_records(self) -> typing.Self:
        """Require each stream state to match its records, and aborts to follow temperatures."""
        if (self.lifecycle_state is ColdLifecycleEvidenceState.ABSENT) != (self.lifecycle == ()):
            raise ValueError("lifecycle state does not match its records")
        if self.advisory_attempt_state is not _advisory_state(self.advisory_attempts):
            raise ValueError("advisory attempt state does not match its records")
        if (self.terminal_state is ColdFailedRunTerminalEvidenceState.ABSENT) != (
            self.terminal is None
        ):
            raise ValueError("terminal state does not match its record")
        if (self.tick_temperature_state is ColdTickTemperatureEvidenceState.ABSENT) != (
            self.tick_temperatures == ()
        ):
            raise ValueError("tick temperature state does not match its records")
        if (
            self.temperature_aborts != ()
            and self.tick_temperature_state is not ColdTickTemperatureEvidenceState.PRESENT
        ):
            raise ValueError("temperature aborts require paired tick temperatures")
        return self


def _advisory_state(
    attempts: tuple[ColdAdvisoryAttemptRecord, ...],
) -> ColdAdvisoryAttemptEvidenceState:
    """Return the structural state implied by retained attempts in run order."""
    if not attempts:
        return ColdAdvisoryAttemptEvidenceState.ABSENT
    if isinstance(attempts[-1], ColdAdvisoryIntentRecord):
        return ColdAdvisoryAttemptEvidenceState.OPEN_TAIL
    return ColdAdvisoryAttemptEvidenceState.COMPLETE


def _closed(failure: ColdEvidenceStoreFailure) -> ColdEvidenceStoreError:
    """Return a fresh closed store error."""
    return ColdEvidenceStoreError(failure)


def _decode_abort(document: dict[str, object]) -> ColdEvidenceRecord:
    """Decode an abort record's enum fields exactly by domain before strict validation."""
    phase_value = document.get("phase")
    domain_value = document.get("domain")
    reason_value = document.get("reason")
    if (
        type(phase_value) is not str
        or type(domain_value) is not str
        or type(reason_value) is not str
    ):
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded: dict[str, object] | None = None
    try:
        domain = ColdAbortDomain(domain_value)
        decoded = {
            **document,
            "phase": ColdPhaseKind(phase_value),
            "domain": domain,
            "reason": ABORT_REASON_BY_DOMAIN[domain](reason_value),
        }
    except ValueError:
        pass
    if decoded is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    return _validate(lambda: _ABORT_ADAPTER.validate_python(decoded, strict=True))


def _validate(call: typing.Callable[[], ColdEvidenceRecord]) -> ColdEvidenceRecord:
    """Run one strict validation, mapping parser errors to a closed failure."""
    try:
        record = call()
    except pydantic.ValidationError:
        pass
    else:
        return record
    raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)


def _decode_line(
    stream: ColdEvidenceStream, document: dict[str, object], line: bytes
) -> ColdEvidenceRecord:
    """Dispatch on the file's stream before any union and strictly validate one line."""
    if stream is ColdEvidenceStream.HEADER:
        return _validate(lambda: _HEADER_ADAPTER.validate_json(line, strict=True))
    if stream is ColdEvidenceStream.TICK:
        return _validate(lambda: _TICK_ADAPTER.validate_json(line, strict=True))
    if stream is ColdEvidenceStream.HOST:
        return _validate(lambda: _HOST_ADAPTER.validate_json(line, strict=True))
    if stream is ColdEvidenceStream.ADVISORY:
        return _validate(lambda: _ADVISORY_ADAPTER.validate_json(line, strict=True))
    if stream is ColdEvidenceStream.FINALISATION:
        return _validate(lambda: _FINALISATION_ADAPTER.validate_json(line, strict=True))
    return _decode_abort(document)


def _read_line(
    line: bytes,
    *,
    phase: ColdPhaseKind,
    stream: ColdEvidenceStream,
    state: ColdBindingState,
) -> ColdEvidenceRecord:
    """Strictly decode, losslessly re-prove, validate, and bind one framed record line.

    The line bound is enforced earlier, incrementally, by ``read_verified_lines``.
    """
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in _SCHEMA_VERSIONS:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or _STREAMS_BY_VALUE.get(stream_value) is not stream:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    validated = _decode_line(stream, document, line)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)
    snapshot = validate_record(validated)
    check_record_binding(state, snapshot, writer_root=None)
    return snapshot


def _read_lifecycle_line(
    line: bytes,
    *,
    phase: ColdPhaseKind,
    state: ColdBindingState,
    sequence: ColdLifecycleSequence,
) -> ColdLifecycleRecord:
    """Strictly decode, losslessly re-prove, validate, bind, and order one lifecycle line."""
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in _LIFECYCLE_SCHEMA_VERSIONS:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or stream_value != _LIFECYCLE_STREAM:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    try:
        validated: ColdLifecycleRecord | None = _LIFECYCLE_ADAPTER.validate_json(line, strict=True)
    except pydantic.ValidationError:
        validated = None
    if validated is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)
    snapshot = validate_lifecycle_record(validated)
    check_lifecycle_binding(state, snapshot)
    sequence.check(snapshot)
    sequence.commit(snapshot)
    return snapshot


def _read_advisory_attempt_line(
    line: bytes,
    *,
    phase: ColdPhaseKind,
    state: ColdBindingState,
    sequence: ColdAdvisorySequence,
) -> ColdAdvisoryAttemptRecord:
    """Strictly decode, losslessly re-prove, validate, bind, and order one attempt line."""
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in _ADVISORY_SCHEMA_VERSIONS:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or stream_value != _ADVISORY_STREAM:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    entry = document.get("entry")
    adapter = _ADVISORY_ADAPTERS.get(entry) if type(entry) is str else None
    validated: ColdAdvisoryAttemptRecord | None = None
    if adapter is not None:
        try:
            validated = adapter.validate_json(line, strict=True)
        except pydantic.ValidationError:
            validated = None
    if validated is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)
    snapshot = validate_advisory_attempt_record(validated)
    check_advisory_attempt_binding(state, snapshot)
    sequence.check(snapshot)
    sequence.commit(snapshot)
    return snapshot


def _read_failed_run_terminal_line(
    line: bytes, *, phase: ColdPhaseKind, state: ColdBindingState
) -> ColdFailedRunTerminalRecord:
    """Strictly decode, losslessly re-prove, validate, and bind one failed-run terminal line."""
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in _TERMINAL_SCHEMA_VERSIONS:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or stream_value != _TERMINAL_STREAM:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    try:
        validated: ColdFailedRunTerminalRecord | None = _TERMINAL_ADAPTER.validate_json(
            line, strict=True
        )
    except pydantic.ValidationError:
        validated = None
    if validated is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)
    snapshot = validate_failed_run_terminal_record(validated)
    check_failed_run_terminal_binding(state, snapshot)
    return snapshot


def _read_tick_temperature_line(
    line: bytes, *, phase: ColdPhaseKind, state: ColdBindingState
) -> ColdTickTemperatureRecord:
    """Strictly decode, losslessly re-prove, validate, and bind one tick-temperature line.

    Precedence is pinned: an empty line, strict JSON as an exact object, the shared
    walker, the exact integer version (which wins over every later malformation),
    the exact stream, the closed decode, the directory phase, the canonical
    re-proof, re-admission, and finally binding.
    """
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in _TICK_TEMPERATURE_SCHEMA_VERSIONS:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or stream_value != _TICK_TEMPERATURE_STREAM:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    validated = decode_tick_temperature_document(document)
    if validated is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)
    snapshot = validate_tick_temperature_record(validated)
    check_tick_temperature_binding(state, snapshot)
    return snapshot


def _read_tick_temperatures(
    tree: ColdVerifiedTree,
    paths: dict[ColdPhaseKind, str],
    *,
    state: ColdBindingState,
    streams: list["ColdRetainedStream"],
) -> tuple[ColdTickTemperatureRecord, ...]:
    """Read every tick-temperature file, then require whole-run positional pairing.

    Files are read in phase order.  A real empty or blank file is refused earlier,
    as ``LINE_MALFORMED``, by the shared line framing; ``STREAM_EMPTY`` is only a
    local defensive guard against a framed read that yields no line.  Once any
    record is read, every phase's already admitted v1 ticks must pair one-to-one,
    in order, with that phase's temperature records (a missing file pairs as empty).
    """
    by_phase: dict[ColdPhaseKind, tuple[ColdTickTemperatureRecord, ...]] = {}
    for phase in ColdPhaseKind:
        path = paths.get(phase)
        if path is None:
            continue
        records = tuple(
            _read_tick_temperature_line(line, phase=phase, state=state)
            for line in read_verified_lines(tree, path, max_line_bytes=MAX_LINE_BYTES)
        )
        if not records:
            raise ColdTickTemperatureError(ColdTickTemperatureFailure.STREAM_EMPTY)
        by_phase[phase] = records
    for phase in ColdPhaseKind:
        ticks = tuple(
            typing.cast(ColdTickRecord, record)
            for stream in streams
            if stream.phase is phase and stream.stream is ColdEvidenceStream.TICK
            for record in stream.records
        )
        check_tick_temperature_pairing(ticks, by_phase.get(phase, ()))
    return tuple(record for records in by_phase.values() for record in records)


def _temperature_run_document(
    line: bytes, *, versions: frozenset[int], stream: str
) -> dict[str, object]:
    """Frame one D209 line up to its exact stream token, in the pinned precedence.

    Precedence: an empty line, strict JSON as an exact object, the shared walker,
    the exact integer version (which wins over every later malformation), then the
    exact stream.
    """
    if not line:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    decoded = load_strict_json(line, malformed=ColdEvidenceStoreFailure.LINE_MALFORMED)
    if type(decoded) is not dict:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    document = typing.cast(dict[str, object], decoded)
    walk_json_value(typing.cast(pydantic.JsonValue, document))
    version = document.get("schema_version")
    if type(version) is not int or version not in versions:
        raise _closed(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    stream_value = document.get("stream")
    if type(stream_value) is not str or stream_value != stream:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    return document


def _require_line(
    validated: ColdTemperatureAbortRecord | ColdMcpCandidateRecord | None,
    *,
    phase: ColdPhaseKind,
    line: bytes,
) -> None:
    """Refuse a failed decode, a foreign directory phase, or a non-canonical line."""
    if validated is None:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if validated.phase is not phase:
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    if canonical_json(validated.model_dump(mode="json")).encode("utf-8") != line:
        raise _closed(ColdEvidenceStoreFailure.LINE_NOT_CANONICAL)


def _read_temperature_abort_line(
    line: bytes, *, phase: ColdPhaseKind, state: ColdBindingState
) -> ColdTemperatureAbortRecord:
    """Strictly decode, losslessly re-prove, re-admit, and bind one temperature-abort line.

    Precedence is pinned as for tick-temperature lines: framing up to the stream,
    the closed decode, the directory phase, the canonical re-proof, re-admission,
    and finally binding.
    """
    document = _temperature_run_document(
        line, versions=_TEMPERATURE_ABORT_SCHEMA_VERSIONS, stream=_TEMPERATURE_ABORT_STREAM
    )
    validated = decode_temperature_abort_document(document)
    _require_line(validated, phase=phase, line=line)
    snapshot = validate_temperature_abort_record(validated)
    check_temperature_run_binding(state, snapshot)
    return snapshot


def _read_mcp_candidate_line(
    line: bytes, *, phase: ColdPhaseKind, state: ColdBindingState
) -> ColdMcpCandidateRecord:
    """Strictly decode, losslessly re-prove, re-admit, and bind one MCP candidate line.

    Precedence is pinned as for tick-temperature lines.
    """
    document = _temperature_run_document(
        line, versions=_MCP_CANDIDATE_SCHEMA_VERSIONS, stream=_MCP_CANDIDATE_STREAM
    )
    validated = decode_mcp_candidate_document(document)
    _require_line(validated, phase=phase, line=line)
    snapshot = validate_mcp_candidate_record(validated)
    check_temperature_run_binding(state, snapshot)
    return snapshot


def _read_temperature_run(
    tree: ColdVerifiedTree,
    layout: "_RecordLayout",
    *,
    state: ColdBindingState,
    streams: list["ColdRetainedStream"],
    tick_temperatures: tuple[ColdTickTemperatureRecord, ...],
) -> tuple[tuple[ColdTemperatureAbortRecord, ...], tuple[ColdMcpCandidateRecord, ...]]:
    """Read every candidate and temperature-abort file in phase order, then check them.

    Each present candidate file must frame exactly one line.  A real empty or blank
    file is refused earlier, as ``LINE_MALFORMED``, by the shared line framing; the
    zero-line case of ``CANDIDATE_NOT_UNIQUE`` is only a local defensive guard.
    Every abort must name a retained tick of its phase while tick temperatures are
    retained (whole-run pairing has already held), and no ``(phase, tick, reason)``
    may repeat.  The reader cannot order lines across files, so it cannot prove a
    candidate preceded the ticks, and it never compares an abort's recording instant
    with its tick's.
    """
    candidates: list[ColdMcpCandidateRecord] = []
    for phase in ColdPhaseKind:
        path = layout.mcp_candidate.get(phase)
        if path is None:
            continue
        records = tuple(
            _read_mcp_candidate_line(line, phase=phase, state=state)
            for line in read_verified_lines(tree, path, max_line_bytes=MAX_LINE_BYTES)
        )
        if len(records) != 1:
            raise ColdTemperatureRunError(ColdTemperatureRunFailure.CANDIDATE_NOT_UNIQUE)
        candidates.extend(records)
    aborts: list[ColdTemperatureAbortRecord] = []
    seen: set[tuple[ColdPhaseKind, int, ColdTemperatureScreenReason]] = set()
    for phase in ColdPhaseKind:
        path = layout.temperature_abort.get(phase)
        if path is None:
            continue
        retained_ticks = {
            typing.cast(ColdTickRecord, record).tick
            for stream in streams
            if stream.phase is phase and stream.stream is ColdEvidenceStream.TICK
            for record in stream.records
        }
        for line in read_verified_lines(tree, path, max_line_bytes=MAX_LINE_BYTES):
            record = _read_temperature_abort_line(line, phase=phase, state=state)
            if tick_temperatures == () or record.tick not in retained_ticks:
                raise ColdTemperatureRunError(ColdTemperatureRunFailure.TICK_NOT_PAIRED)
            key = (record.phase, record.tick, record.reason)
            if key in seen:
                raise ColdTemperatureRunError(ColdTemperatureRunFailure.ABORT_DUPLICATED)
            seen.add(key)
            aborts.append(record)
    return tuple(aborts), tuple(candidates)


class _RecordLayout(typing.NamedTuple):
    """Every ``records/`` entry mapped to its closed phase and stream or extra file."""

    streams: dict[ColdPhaseKind, dict[ColdEvidenceStream, str]]
    lifecycle: dict[ColdPhaseKind, str]
    advisory: dict[ColdPhaseKind, str]
    terminal: dict[ColdPhaseKind, str]
    tick_temperature: dict[ColdPhaseKind, str]
    temperature_abort: dict[ColdPhaseKind, str]
    mcp_candidate: dict[ColdPhaseKind, str]


def _record_layout(tree: ColdVerifiedTree, *, profile: _ReadProfile) -> _RecordLayout:
    """Map every ``records/`` entry to its closed phase and stream, refusing others.

    Only the exact extra file names the profile admits map to a phase's lifecycle,
    advisory-attempt, failed-run terminal, or tick-temperature stream; every other
    unknown entry is refused.
    """
    layout = _RecordLayout({}, {}, {}, {}, {}, {}, {})
    extras = {
        _LIFECYCLE_FILE_NAME: layout.lifecycle,
        _ADVISORY_FILE_NAME: layout.advisory,
        _TERMINAL_FILE_NAME: layout.terminal,
        _TICK_TEMPERATURE_FILE_NAME: layout.tick_temperature,
        _TEMPERATURE_ABORT_FILE_NAME: layout.temperature_abort,
        _MCP_CANDIDATE_FILE_NAME: layout.mcp_candidate,
    }
    for entry in tree.manifest.entries:
        segments = entry.relative_path.split("/")
        if segments[0] != _RECORDS_DIRECTORY:
            continue
        phase = _PHASES_BY_NAME.get(segments[1]) if len(segments) == 3 else None
        if phase is not None and segments[2] in profile.value:
            extras[segments[2]][phase] = entry.relative_path
            continue
        stream = _STREAMS_BY_FILE_NAME.get(segments[2]) if len(segments) == 3 else None
        if phase is None or stream is None:
            raise _closed(ColdEvidenceStoreFailure.ENTRY_PATH_INVALID)
        layout.streams.setdefault(phase, {})[stream] = entry.relative_path
    return layout


class _VerifiedRead(typing.NamedTuple):
    """The single shared read loop's result, before any profile-specific carrier."""

    run: ColdRetainedRun
    lifecycle: tuple[ColdLifecycleRecord, ...]
    lifecycle_present: bool
    advisory: tuple[ColdAdvisoryAttemptRecord, ...]
    terminal: ColdFailedRunTerminalRecord | None
    tick_temperatures: tuple[ColdTickTemperatureRecord, ...]
    temperature_aborts: tuple[ColdTemperatureAbortRecord, ...]
    mcp_candidates: tuple[ColdMcpCandidateRecord, ...]


def _read_verified_run(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...],
    profile: _ReadProfile,
) -> _VerifiedRead:
    """Verify one tree, then strictly read every record; the single shared read loop."""
    if not run_id_is_valid(run_id):
        raise _closed(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    tree = verify_retained_tree(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
    )
    layout = _record_layout(tree, profile=profile)
    state = ColdBindingState(run_id)
    order = ColdLifecycleSequence()
    attempt_order = ColdAdvisorySequence()
    streams: list[ColdRetainedStream] = []
    lifecycle: list[ColdLifecycleRecord] = []
    advisory: list[ColdAdvisoryAttemptRecord] = []
    terminals: list[ColdFailedRunTerminalRecord] = []
    for phase in ColdPhaseKind:
        files = layout.streams.get(phase, {})
        lifecycle_path = layout.lifecycle.get(phase)
        advisory_path = layout.advisory.get(phase)
        terminal_path = layout.terminal.get(phase)
        if not files and lifecycle_path is None and advisory_path is None and terminal_path is None:
            continue
        if ColdEvidenceStream.HEADER not in files:
            raise _closed(ColdEvidenceStoreFailure.HEADER_MISSING)
        for stream in ColdEvidenceStream:
            path = files.get(stream)
            if path is None:
                continue
            records = tuple(
                _read_line(line, phase=phase, stream=stream, state=state)
                for line in read_verified_lines(tree, path, max_line_bytes=MAX_LINE_BYTES)
            )
            streams.append(ColdRetainedStream(phase=phase, stream=stream, records=records))
        if lifecycle_path is not None:
            lifecycle.extend(
                _read_lifecycle_line(line, phase=phase, state=state, sequence=order)
                for line in read_verified_lines(tree, lifecycle_path, max_line_bytes=MAX_LINE_BYTES)
            )
        if advisory_path is not None:
            advisory.extend(
                _read_advisory_attempt_line(line, phase=phase, state=state, sequence=attempt_order)
                for line in read_verified_lines(tree, advisory_path, max_line_bytes=MAX_LINE_BYTES)
            )
        if terminal_path is not None:
            terminals.extend(
                _read_failed_run_terminal_line(line, phase=phase, state=state)
                for line in read_verified_lines(tree, terminal_path, max_line_bytes=MAX_LINE_BYTES)
            )
    if len(terminals) > 1:
        raise ColdFailedRunTerminalError(ColdFailedRunTerminalFailure.TERMINAL_DUPLICATED)
    terminal = terminals[0] if terminals else None
    if terminal is not None:
        check_failed_run_terminal_order(
            terminal,
            latest_phase=state.headers[-1][0].phase,
            lifecycle_terminated=order.terminated,
            lifecycle_records=order.next_sequence,
            advisory_records=len(advisory),
        )
    bound = {(header.phase, header.identity_sha256) for header, _identity in state.headers}
    recorded = {(item.phase, item.identity_sha256) for item in tree.manifest.identity_bindings}
    if bound != recorded:
        raise _closed(ColdEvidenceStoreFailure.HEADER_BINDING_MISMATCHED)
    tick_temperatures: tuple[ColdTickTemperatureRecord, ...] = ()
    if layout.tick_temperature:
        tick_temperatures = _read_tick_temperatures(
            tree, layout.tick_temperature, state=state, streams=streams
        )
    temperature_aborts: tuple[ColdTemperatureAbortRecord, ...] = ()
    mcp_candidates: tuple[ColdMcpCandidateRecord, ...] = ()
    if layout.temperature_abort or layout.mcp_candidate:
        temperature_aborts, mcp_candidates = _read_temperature_run(
            tree, layout, state=state, streams=streams, tick_temperatures=tick_temperatures
        )
    run = ColdRetainedRun(
        run_id=run_id,
        manifest_sha256=tree.manifest_sha256,
        headers=tuple(
            ColdRetainedHeader(header=header, identity=identity)
            for header, identity in state.headers
        ),
        streams=tuple(streams),
    )
    return _VerifiedRead(
        run,
        tuple(lifecycle),
        bool(layout.lifecycle),
        tuple(advisory),
        terminal,
        tick_temperatures,
        temperature_aborts,
        mcp_candidates,
    )


def read_retained_run(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRun:
    """Verify one retained tree, then strictly and losslessly read every record.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The retained run: headers with v1 identities and per-stream records in file order.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
    """
    return _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V1,
    ).run


def read_retained_run_v2(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRunV2:
    """Verify one retained tree, then strictly read every v1 record and v2 lifecycle line.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    The whole tree is verified before anything is parsed.  v1 streams accept schema
    version 1 only; ``lifecycle.jsonl`` accepts version 2 only; any other
    ``records/`` entry is refused, never ignored.  Entries outside ``records/`` stay
    verified but unparsed (the ratified 3B-ii-b policy).  This reader never calls the
    v1 reader.  A tree with no lifecycle stream reads as ``ABSENT``, which is never
    healthy; a missing terminal record is returned as data, not refused.  The
    returned ``run`` is the same type the v1 reader returns.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The retained run, its lifecycle state, and lifecycle records in run order.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
        ColdLifecycleError: If lifecycle records break the run-wide order.
    """
    read = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V2,
    )
    return ColdRetainedRunV2(
        run=read.run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT
            if read.lifecycle_present
            else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=read.lifecycle,
    )


def read_retained_run_v3(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRunV3:
    """Verify one retained tree, then strictly read v1 records, lifecycle, and attempts.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    The whole tree is verified before anything is parsed.  ``lifecycle.jsonl`` and
    ``advisory_attempt.jsonl`` each accept their own per-stream version 2 only; any
    other ``records/`` entry is refused.  This reader never calls the v1 or v2
    reader and nests no v2 carrier.  ``ABSENT`` and ``OPEN_TAIL`` are never healthy,
    and the result is the input to advisory conformance policy 2
    (``check_advisory_conformance``); a hand-built instance carries no manifest
    provenance.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The retained run, lifecycle state and records, and attempt state and records.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
        ColdLifecycleError: If lifecycle records break the run-wide order.
        ColdAdvisoryAttemptError: If attempt records break the run-wide order.
    """
    read = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V3,
    )
    return ColdRetainedRunV3(
        run=read.run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT
            if read.lifecycle_present
            else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=read.lifecycle,
        advisory_attempt_state=_advisory_state(read.advisory),
        advisory_attempts=read.advisory,
    )


def read_retained_run_v4(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRunV4:
    """Verify one retained tree, then strictly read v1 records, lifecycle, attempts, and terminal.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    The whole tree is verified before anything is parsed.  ``lifecycle.jsonl`` and
    ``advisory_attempt.jsonl`` accept their per-stream version 2 only and
    ``failed_run_terminal.jsonl`` its per-stream version 3 only; any other
    ``records/`` entry is refused.  At most one terminal line is admitted, in the
    latest bound phase, after no v2 run termination, and its counts must equal the
    retained lifecycle and advisory attempt lines.  This reader never calls the v1,
    v2, or v3 reader and nests no v2 or v3 carrier; the result is the input to no
    conformance policy, and a terminal never qualifies a run.

    Honest limits: the reader cannot order lines across files.  Lifecycle or
    advisory lines added after the terminal are detected only through its counts,
    and a later phase only through ``PHASE_NOT_LATEST``; writer refusal is the guard
    for v1-stream lines added after the terminal.  A v1 tick or host line written
    after the terminal into a tree forged with a re-crafted manifest is not
    detectable here: the trusted manifest digest is the provenance boundary.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The retained run, lifecycle and attempt states and records, and the terminal.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
        ColdLifecycleError: If lifecycle records break the run-wide order.
        ColdAdvisoryAttemptError: If attempt records break the run-wide order.
        ColdFailedRunTerminalError: If terminals are duplicated or contradict the run.
    """
    read = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V4,
    )
    return ColdRetainedRunV4(
        run=read.run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT
            if read.lifecycle_present
            else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=read.lifecycle,
        advisory_attempt_state=_advisory_state(read.advisory),
        advisory_attempts=read.advisory,
        terminal_state=(
            ColdFailedRunTerminalEvidenceState.ABSENT
            if read.terminal is None
            else ColdFailedRunTerminalEvidenceState.PRESENT
        ),
        terminal=read.terminal,
    )


def read_retained_run_v5(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRunV5:
    """Verify one retained tree, then strictly read V4's streams and tick temperatures.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    The whole tree is verified before anything is parsed.  The V4 grammar, order,
    and terminal rules are unchanged, and ``tick_temperature.jsonl`` accepts its
    per-stream version 4 only; any other ``records/`` entry is refused.  After the
    shared read and the manifest binding check, every tick-temperature file is read
    in phase order (a real empty or blank file is refused as ``LINE_MALFORMED`` by
    the shared line framing) and, across every phase, each retained v1 tick must
    pair one-to-one and in order with one temperature record.
    This reader never calls an earlier reader and nests no earlier carrier; the
    result is integrity data and the input to no policy.  A tree with no
    tick-temperature file reads as ``ABSENT``, which is never proof of origin and
    never satisfies screening.

    Honest limits: pairing is positional within each phase, and the reader cannot
    order lines across files.  Temperature lines added after a failed-run terminal
    are not counted by the terminal; writer refusal after the terminal is the guard.
    Digest verification detects alteration relative to the supplied digest; it
    proves neither authenticity nor installed bytes.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The V4 streams and states plus the tick-temperature state and records.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
        ColdLifecycleError: If lifecycle records break the run-wide order.
        ColdAdvisoryAttemptError: If attempt records break the run-wide order.
        ColdFailedRunTerminalError: If terminals are duplicated or contradict the run.
        ColdTickTemperatureError: If the records do not pair positionally with the
            retained ticks, or (defensively) a framed read yields no line.
    """
    read = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V5,
    )
    return ColdRetainedRunV5(
        run=read.run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT
            if read.lifecycle_present
            else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=read.lifecycle,
        advisory_attempt_state=_advisory_state(read.advisory),
        advisory_attempts=read.advisory,
        terminal_state=(
            ColdFailedRunTerminalEvidenceState.ABSENT
            if read.terminal is None
            else ColdFailedRunTerminalEvidenceState.PRESENT
        ),
        terminal=read.terminal,
        tick_temperature_state=(
            ColdTickTemperatureEvidenceState.ABSENT
            if read.tick_temperatures == ()
            else ColdTickTemperatureEvidenceState.PRESENT
        ),
        tick_temperatures=read.tick_temperatures,
    )


def read_retained_run_v6(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRunV6:
    """Verify one retained tree, then strictly read V5's streams and the D209 run evidence.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    The whole tree is verified before anything is parsed.  The V5 grammar, order,
    terminal, and whole-run tick-temperature pairing rules are unchanged;
    ``temperature_abort.jsonl`` accepts its per-stream version 5 only and
    ``mcp_candidate.jsonl`` its per-stream version 6 only; any other ``records/``
    entry is refused.  After tick-temperature pairing, each present candidate file
    must hold exactly one line, every abort must name a retained tick of its phase
    while tick temperatures are retained, and no ``(phase, tick, reason)`` may repeat.
    This reader never calls an earlier reader and nests no earlier carrier; the
    result is integrity data and the sole input to temperature conformance policy 3.

    Honest limits: the reader cannot order lines across files, so it cannot prove a
    candidate was appended before the phase's ticks; an abort's recording instant is
    never compared with its tick's.  A candidate is an operator assertion; digest
    verification detects alteration relative to the supplied digest and proves
    neither authenticity nor installed bytes.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The V5 streams and states plus the temperature aborts and MCP candidates.

    Raises:
        ColdEvidenceStoreError: If verification, decoding, or binding fails.
        ColdEvidenceError: If a decoded record fails schema revalidation.
        ColdLifecycleError: If lifecycle records break the run-wide order.
        ColdAdvisoryAttemptError: If attempt records break the run-wide order.
        ColdFailedRunTerminalError: If terminals are duplicated or contradict the run.
        ColdTickTemperatureError: If tick temperatures do not pair with the ticks.
        ColdTemperatureRunError: If a candidate is not unique in its file, or an
            abort names no retained paired tick or repeats a reason.
    """
    read = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        profile=_ReadProfile.V6,
    )
    return ColdRetainedRunV6(
        run=read.run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT
            if read.lifecycle_present
            else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=read.lifecycle,
        advisory_attempt_state=_advisory_state(read.advisory),
        advisory_attempts=read.advisory,
        terminal_state=(
            ColdFailedRunTerminalEvidenceState.ABSENT
            if read.terminal is None
            else ColdFailedRunTerminalEvidenceState.PRESENT
        ),
        terminal=read.terminal,
        tick_temperature_state=(
            ColdTickTemperatureEvidenceState.ABSENT
            if read.tick_temperatures == ()
            else ColdTickTemperatureEvidenceState.PRESENT
        ),
        tick_temperatures=read.tick_temperatures,
        temperature_aborts=read.temperature_aborts,
        mcp_candidates=read.mcp_candidates,
    )
