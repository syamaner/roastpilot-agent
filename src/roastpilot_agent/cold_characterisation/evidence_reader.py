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
    check_lifecycle_binding,
    check_record_binding,
    load_strict_json,
    read_verified_lines,
    run_id_is_valid,
    verify_retained_tree,
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


class _ReadProfile(enum.Enum):
    """Closed reader profiles; each value is the immutable set of extra file names admitted.

    A profile is a reader grammar, not a record schema version.
    """

    V1 = frozenset[str]()
    V2 = frozenset({_LIFECYCLE_FILE_NAME})
    V3 = frozenset({_LIFECYCLE_FILE_NAME, _ADVISORY_FILE_NAME})


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
    possibly as failed, abandoned, or unresolved at phase end.  It is not a
    conformance input.
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


class _RecordLayout(typing.NamedTuple):
    """Every ``records/`` entry mapped to its closed phase and stream or extra file."""

    streams: dict[ColdPhaseKind, dict[ColdEvidenceStream, str]]
    lifecycle: dict[ColdPhaseKind, str]
    advisory: dict[ColdPhaseKind, str]


def _record_layout(tree: ColdVerifiedTree, *, profile: _ReadProfile) -> _RecordLayout:
    """Map every ``records/`` entry to its closed phase and stream, refusing others.

    Only the exact extra file names the profile admits map to a phase's lifecycle
    or advisory-attempt stream; every other unknown entry is refused.
    """
    layout = _RecordLayout({}, {}, {})
    extras = {_LIFECYCLE_FILE_NAME: layout.lifecycle, _ADVISORY_FILE_NAME: layout.advisory}
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
    for phase in ColdPhaseKind:
        files = layout.streams.get(phase, {})
        lifecycle_path = layout.lifecycle.get(phase)
        advisory_path = layout.advisory.get(phase)
        if not files and lifecycle_path is None and advisory_path is None:
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
    bound = {(header.phase, header.identity_sha256) for header, _identity in state.headers}
    recorded = {(item.phase, item.identity_sha256) for item in tree.manifest.identity_bindings}
    if bound != recorded:
        raise _closed(ColdEvidenceStoreFailure.HEADER_BINDING_MISMATCHED)
    run = ColdRetainedRun(
        run_id=run_id,
        manifest_sha256=tree.manifest_sha256,
        headers=tuple(
            ColdRetainedHeader(header=header, identity=identity)
            for header, identity in state.headers
        ),
        streams=tuple(streams),
    )
    return _VerifiedRead(run, tuple(lifecycle), bool(layout.lifecycle), tuple(advisory))


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
    and the result is not a conformance input.

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
