"""Contained construction of cold-characterisation evidence records.

Every builder inherits run id, phase, and identity digest from the phase header
it binds to, and returns only after ``validate_record``.  Builders compute no
outcome and never call the D195 clean conjunction.
"""

import hashlib
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdEnvelopeKind,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdEvidenceRecord,
    ColdFinalisationRecord,
    ColdHostRecord,
    ColdHostSample,
    ColdPhaseKind,
    ColdRunHeader,
    ColdSealedEnvelope,
    ColdTickProjection,
    ColdTickRecord,
    validate_record,
    walk_json_value,
)
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdEvidenceStoreError,
    ColdEvidenceStoreFailure,
    canonical_json,
    derive_finalisation_index,
    parse_finalisation_envelope,
    run_id_is_valid,
)
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import ColdRunIdentity, identity_sha256
from roastpilot_agent.cold_characterisation.mcp import SessionFinalisationResult
from roastpilot_agent.mcp_client import RoasterDeviceState

_RecordT = typing.TypeVar(
    "_RecordT", ColdRunHeader, ColdTickRecord, ColdHostRecord, ColdFinalisationRecord
)


def _require_type(value: object, expected: type[object]) -> None:
    """Refuse any value that is not exactly the expected in-process type."""
    if type(value) is not expected:
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def _construct(build: typing.Callable[[], _RecordT], expected: type[_RecordT]) -> _RecordT:
    """Construct one record, then return only its ``validate_record`` snapshot."""
    try:
        record = build()
    except pydantic.ValidationError:
        pass
    else:
        snapshot: ColdEvidenceRecord = validate_record(record)
        if type(snapshot) is not expected:  # pragma: no cover - validate_record keeps the class.
            raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
        return typing.cast(_RecordT, snapshot)
    raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def _envelope(kind: ColdEnvelopeKind, canonical: str, digest: str) -> ColdSealedEnvelope:
    """Build one sealed envelope over exact canonical text."""
    try:
        envelope = ColdSealedEnvelope(
            kind=kind,
            schema_version=1,
            canonical_json=canonical,
            canonical_byte_length=len(canonical.encode("utf-8")),
            sha256=digest,
        )
    except pydantic.ValidationError:
        pass
    else:
        return envelope
    raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def build_run_header(
    *,
    identity: ColdRunIdentity,
    phase: ColdPhaseKind,
    recorded_at_utc: str,
    monotonic_seconds: float,
) -> ColdRunHeader:
    """Build one phase header whose envelope is the identity's exact digest input.

    Args:
        identity: The frozen phase identity.
        phase: The phase this header opens.
        recorded_at_utc: Recording timestamp.
        monotonic_seconds: Recording monotonic time.

    Returns:
        The validated header snapshot.

    Raises:
        ColdEvidenceError: If the identity or header fails schema admission.
        ColdEvidenceStoreError: If the run id or digest does not bind.
    """
    _require_type(identity, ColdRunIdentity)
    if not run_id_is_valid(identity.run_id):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    dumped = identity.model_dump(mode="json")
    walk_json_value(dumped)
    canonical = canonical_json(dumped)
    digest = identity_sha256(identity)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_DIGEST_MISMATCHED)
    envelope = _envelope(ColdEnvelopeKind.IDENTITY, canonical, digest)
    return _construct(
        lambda: ColdRunHeader(
            schema_version=1,
            stream="header",
            run_id=identity.run_id,
            phase=phase,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            identity_sha256=digest,
            identity=envelope,
        ),
        ColdRunHeader,
    )


def build_tick_record(
    *,
    header: ColdRunHeader,
    tick: int,
    recorded_at_utc: str,
    monotonic_seconds: float,
    device_state: RoasterDeviceState,
    projection: ColdTickProjection,
) -> ColdTickRecord:
    """Build one tick record retaining raw audio extras and raw vendor data.

    Args:
        header: The bound phase header.
        tick: Tick index.
        recorded_at_utc: Recording timestamp.
        monotonic_seconds: Recording monotonic time.
        device_state: Typed MCP device state; six fields and vendor data are copied.
        projection: Strict audio projection; its raw extras are always retained.

    Returns:
        The validated tick snapshot; oversized input refuses and is never truncated.

    Raises:
        ColdEvidenceError: If any input or the record fails admission.
    """
    _require_type(header, ColdRunHeader)
    _require_type(device_state, RoasterDeviceState)
    _require_type(projection, ColdTickProjection)
    return _construct(
        lambda: ColdTickRecord(
            schema_version=1,
            stream="tick",
            run_id=header.run_id,
            phase=header.phase,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            identity_sha256=header.identity_sha256,
            tick=tick,
            bean_temp_c=device_state.bean_temp_c,
            env_temp_c=device_state.env_temp_c,
            heat_level_percent=device_state.heat_level_percent,
            fan_level_percent=device_state.fan_level_percent,
            cooling_on=device_state.cooling_on,
            connected=device_state.connected,
            audio=projection.audio,
            raw_audio_extra=dict(projection.raw_audio_extra),
            raw_vendor_data=dict(device_state.raw_vendor_data),
        ),
        ColdTickRecord,
    )


def build_host_record(
    *,
    header: ColdRunHeader,
    sample: HostBoundSample,
    recorded_at_utc: str,
    monotonic_seconds: float,
) -> ColdHostRecord:
    """Build one host record copying the six host-bound sample fields.

    Args:
        header: The bound phase header.
        sample: One complete host-bound sample.
        recorded_at_utc: Recording timestamp.
        monotonic_seconds: Recording monotonic time.

    Returns:
        The validated host snapshot.

    Raises:
        ColdEvidenceError: If any input or the record fails admission.
    """
    _require_type(header, ColdRunHeader)
    _require_type(sample, HostBoundSample)
    return _construct(
        lambda: ColdHostRecord(
            schema_version=1,
            stream="host",
            run_id=header.run_id,
            phase=header.phase,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            identity_sha256=header.identity_sha256,
            sample=ColdHostSample(
                captured_at_utc=sample.captured_at_utc,
                monotonic_seconds=sample.monotonic_seconds,
                soc_temp_c=sample.soc_temp_c,
                throttled_word_hex=sample.throttled_word_hex,
                mem_available_bytes=sample.mem_available_bytes,
                free_bytes=sample.free_bytes,
            ),
        ),
        ColdHostRecord,
    )


def build_finalisation_record(
    *,
    header: ColdRunHeader,
    result: SessionFinalisationResult,
    recorded_at_utc: str,
    monotonic_seconds: float,
) -> ColdFinalisationRecord:
    """Build one finalisation record whose index is derived from its own envelope.

    Args:
        header: The bound phase header.
        result: The strictly parsed MCP finalisation result.
        recorded_at_utc: Recording timestamp.
        monotonic_seconds: Recording monotonic time.

    Returns:
        The validated finalisation snapshot.

    Raises:
        ColdEvidenceError: If any input or the record fails admission.
        ColdEvidenceStoreError: If the envelope does not round-trip to the result.
    """
    _require_type(header, ColdRunHeader)
    _require_type(result, SessionFinalisationResult)
    dumped = result.model_dump(mode="json")
    walk_json_value(dumped)
    canonical = canonical_json(dumped)
    envelope = _envelope(
        ColdEnvelopeKind.FINALISATION,
        canonical,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
    reparsed = parse_finalisation_envelope(envelope)
    if reparsed != result:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FINALISATION_INDEX_MISMATCHED)
    index = derive_finalisation_index(reparsed)
    return _construct(
        lambda: ColdFinalisationRecord(
            schema_version=1,
            stream="finalisation",
            run_id=header.run_id,
            phase=header.phase,
            recorded_at_utc=recorded_at_utc,
            monotonic_seconds=monotonic_seconds,
            identity_sha256=header.identity_sha256,
            session_id=index.session_id,
            envelope=envelope,
            status=index.status,
            clean=index.clean,
            observed_command_streaming_required=index.observed_command_streaming_required,
            applied_branch=index.applied_branch,
        ),
        ColdFinalisationRecord,
    )
