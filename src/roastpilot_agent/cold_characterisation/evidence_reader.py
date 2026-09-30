"""Versioned strict reader for verified retained cold-characterisation evidence.

Unverified bytes are never parsed: the tree is verified against its expected
manifest digest first, and every record file is re-read under identity checks
and required to match its manifest digest before any line is decoded.  The
reader returns integrity-checked records only; it computes no outcome.
"""

import enum
import typing

import pydantic

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


def _record_layout(
    tree: ColdVerifiedTree, *, admit_lifecycle: bool
) -> tuple[dict[ColdPhaseKind, dict[ColdEvidenceStream, str]], dict[ColdPhaseKind, str]]:
    """Map every ``records/`` entry to its closed phase and stream, refusing others.

    Only when ``admit_lifecycle`` is set does the exact name ``lifecycle.jsonl`` map
    to a phase's lifecycle stream; every other unknown entry is refused.
    """
    layout: dict[ColdPhaseKind, dict[ColdEvidenceStream, str]] = {}
    lifecycle: dict[ColdPhaseKind, str] = {}
    for entry in tree.manifest.entries:
        segments = entry.relative_path.split("/")
        if segments[0] != _RECORDS_DIRECTORY:
            continue
        phase = _PHASES_BY_NAME.get(segments[1]) if len(segments) == 3 else None
        if phase is not None and admit_lifecycle and segments[2] == _LIFECYCLE_FILE_NAME:
            lifecycle[phase] = entry.relative_path
            continue
        stream = _STREAMS_BY_FILE_NAME.get(segments[2]) if len(segments) == 3 else None
        if phase is None or stream is None:
            raise _closed(ColdEvidenceStoreFailure.ENTRY_PATH_INVALID)
        layout.setdefault(phase, {})[stream] = entry.relative_path
    return layout, lifecycle


def _read_verified_run(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...],
    admit_lifecycle: bool,
) -> tuple[ColdRetainedRun, tuple[ColdLifecycleRecord, ...], bool]:
    """Verify one tree, then strictly read every record; the single shared read loop."""
    if not run_id_is_valid(run_id):
        raise _closed(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    tree = verify_retained_tree(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
    )
    layout, lifecycle_paths = _record_layout(tree, admit_lifecycle=admit_lifecycle)
    state = ColdBindingState(run_id)
    order = ColdLifecycleSequence()
    streams: list[ColdRetainedStream] = []
    lifecycle: list[ColdLifecycleRecord] = []
    for phase in ColdPhaseKind:
        files = layout.get(phase, {})
        lifecycle_path = lifecycle_paths.get(phase)
        if not files and lifecycle_path is None:
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
    return run, tuple(lifecycle), bool(lifecycle_paths)


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
        admit_lifecycle=False,
    )[0]


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
    run, lifecycle, present = _read_verified_run(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
        admit_lifecycle=True,
    )
    return ColdRetainedRunV2(
        run=run,
        lifecycle_state=(
            ColdLifecycleEvidenceState.PRESENT if present else ColdLifecycleEvidenceState.ABSENT
        ),
        lifecycle=lifecycle,
    )
