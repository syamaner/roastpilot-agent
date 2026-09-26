"""Versioned strict reader for verified retained cold-characterisation evidence.

Unverified bytes are never parsed: the tree is verified against its expected
manifest digest first, and every record file is re-read under identity checks
and required to match its manifest digest before any line is decoded.  The
reader returns integrity-checked records only; it computes no outcome.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_RECORD_BYTES,
    ColdAbortDomain,
    ColdAbortRecord,
    ColdAdvisorFailureKind,
    ColdAdvisoryRecord,
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
    check_record_binding,
    load_strict_json,
    read_verified_file,
    verify_retained_tree,
)

MAX_LINE_BYTES = MAX_RECORD_BYTES + 1
_RECORDS_DIRECTORY = "records"
_SCHEMA_VERSIONS = frozenset({1})
_PHASES_BY_NAME = {phase.value: phase for phase in ColdPhaseKind}
_STREAMS_BY_FILE_NAME = {f"{stream.value}.jsonl": stream for stream in ColdEvidenceStream}
_STREAMS_BY_VALUE = {stream.value: stream for stream in ColdEvidenceStream}

#: Reader-local abort pairing; a contract test pins it to ``ColdAbortRecord``'s validator.
ABORT_REASON_BY_DOMAIN: dict[ColdAbortDomain, type[enum.Enum]] = {
    ColdAbortDomain.HOST: ColdHostAbortReason,
    ColdAbortDomain.IDENTITY: ColdIdentityAbortReason,
    ColdAbortDomain.EVIDENCE: ColdEvidenceFailure,
    ColdAbortDomain.MCP: ColdMcpAbortReason,
    ColdAbortDomain.ADVISOR: ColdAdvisorFailureKind,
    ColdAbortDomain.OPERATOR: ColdOperatorAbortReason,
}

_HEADER_ADAPTER = pydantic.TypeAdapter(ColdRunHeader)
_TICK_ADAPTER = pydantic.TypeAdapter(ColdTickRecord)
_HOST_ADAPTER = pydantic.TypeAdapter(ColdHostRecord)
_ADVISORY_ADAPTER = pydantic.TypeAdapter(ColdAdvisoryRecord)
_FINALISATION_ADAPTER = pydantic.TypeAdapter(ColdFinalisationRecord)
_ABORT_ADAPTER = pydantic.TypeAdapter(ColdAbortRecord)


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
    """Strictly decode, losslessly re-prove, validate, and bind one record line."""
    if len(line) + 1 > MAX_LINE_BYTES:
        raise _closed(ColdEvidenceStoreFailure.LINE_TOO_LARGE)
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


def _split_lines(data: bytes) -> list[bytes]:
    """Split a record file into LF-terminated lines, refusing torn or empty content."""
    if not data or not data.endswith(b"\n"):
        raise _closed(ColdEvidenceStoreFailure.LINE_MALFORMED)
    return data[:-1].split(b"\n")


def _record_layout(tree: ColdVerifiedTree) -> dict[ColdPhaseKind, dict[ColdEvidenceStream, str]]:
    """Map every ``records/`` entry to its closed phase and stream, refusing others."""
    layout: dict[ColdPhaseKind, dict[ColdEvidenceStream, str]] = {}
    for entry in tree.manifest.entries:
        segments = entry.relative_path.split("/")
        if segments[0] != _RECORDS_DIRECTORY:
            continue
        phase = _PHASES_BY_NAME.get(segments[1]) if len(segments) == 3 else None
        stream = _STREAMS_BY_FILE_NAME.get(segments[2]) if len(segments) == 3 else None
        if phase is None or stream is None:
            raise _closed(ColdEvidenceStoreFailure.ENTRY_PATH_INVALID)
        layout.setdefault(phase, {})[stream] = entry.relative_path
    return layout


def read_retained_run(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdRetainedRun:
    """Verify one retained tree, then strictly and losslessly read every record.

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
    tree = verify_retained_tree(
        root,
        run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        protected_roots=protected_roots,
    )
    layout = _record_layout(tree)
    state = ColdBindingState(run_id)
    streams: list[ColdRetainedStream] = []
    for phase in ColdPhaseKind:
        files = layout.get(phase)
        if files is None:
            continue
        if ColdEvidenceStream.HEADER not in files:
            raise _closed(ColdEvidenceStoreFailure.HEADER_MISSING)
        for stream in ColdEvidenceStream:
            path = files.get(stream)
            if path is None:
                continue
            records = tuple(
                _read_line(line, phase=phase, stream=stream, state=state)
                for line in _split_lines(read_verified_file(tree, path))
            )
            streams.append(ColdRetainedStream(phase=phase, stream=stream, records=records))
    bound = {(header.phase, header.identity_sha256) for header, _identity in state.headers}
    recorded = {(item.phase, item.identity_sha256) for item in tree.manifest.identity_bindings}
    if bound != recorded:
        raise _closed(ColdEvidenceStoreFailure.HEADER_BINDING_MISMATCHED)
    return ColdRetainedRun(
        run_id=run_id,
        manifest_sha256=tree.manifest_sha256,
        headers=tuple(
            ColdRetainedHeader(header=header, identity=identity)
            for header, identity in state.headers
        ),
        streams=tuple(streams),
    )
