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
    ColdTickDeviceEvidence,
    ColdTickProjection,
    ColdTickRecord,
    ColdTickRoastFanEvidence,
    ColdTickRoastFanOutcome,
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
from roastpilot_agent.cold_characterisation.mcp import (
    ColdTickDeviceState,
    ColdTickObservation,
    ColdTickRoastFanObservation,
    SessionFinalisationResult,
)

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


def _roast_fan_member(value: object) -> ColdTickRoastFanOutcome:
    """Map one MCP roast-fan enum value to its schema-owned member by exact value."""
    member = typing.cast(object, ColdTickRoastFanOutcome._value2member_map_.get(value))
    if type(member) is not ColdTickRoastFanOutcome:  # pragma: no cover - value sets pinned equal.
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    return member


def build_tick_record(
    *,
    header: ColdRunHeader,
    tick: int,
    recorded_at_utc: str,
    monotonic_seconds: float,
    observation: ColdTickObservation,
) -> ColdTickRecord:
    """Build one tick record from the strict parts of one cold observation.

    Device, roast-fan, and audio evidence come only from the observation's
    strict ``device``, ``roast_fan``, and ``audio`` projections of one MCP
    response.  A ``None`` device is recorded as ``None``; it is never defaulted.
    Values are recorded exactly and judged nowhere here.

    Args:
        header: The bound phase header.
        tick: Tick index.
        recorded_at_utc: Recording timestamp.
        monotonic_seconds: Recording monotonic time.
        observation: One identity-bound strict cold tick observation.

    Returns:
        The validated tick snapshot; oversized input refuses and is never shortened.

    Raises:
        ColdEvidenceError: If any input or the record fails admission.
    """
    _require_type(header, ColdRunHeader)
    _require_type(observation, ColdTickObservation)
    projection = observation.audio
    source_device = observation.device
    source_fan = observation.roast_fan
    _require_type(projection, ColdTickProjection)
    _require_type(source_fan, ColdTickRoastFanObservation)
    if source_device is not None:
        _require_type(source_device, ColdTickDeviceState)

    def _device() -> ColdTickDeviceEvidence | None:
        if source_device is None:
            return None
        return ColdTickDeviceEvidence(
            driver=source_device.driver,
            connected=source_device.connected,
            bean_temp_c=source_device.bean_temp_c,
            env_temp_c=source_device.env_temp_c,
            heat_level_percent=source_device.heat_level_percent,
            fan_level_percent=source_device.fan_level_percent,
            cooling_on=source_device.cooling_on,
            raw_vendor_data=dict(source_device.raw_vendor_data),
        )

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
            device=_device(),
            roast_fan=ColdTickRoastFanEvidence(
                outcome=_roast_fan_member(observation.roast_fan.outcome.value),
                roast_fan_level_percent=source_fan.roast_fan_level_percent,
            ),
            audio=projection.audio,
            raw_audio_extra=dict(projection.raw_audio_extra),
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
