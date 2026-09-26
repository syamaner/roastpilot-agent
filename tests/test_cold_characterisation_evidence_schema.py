"""Behavioural tests for the closed cold evidence schema boundary."""

import ast
import hashlib
import inspect
import json
import math
import pathlib
import typing

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import evidence_schema as evidence
from roastpilot_agent.cold_characterisation.host import ColdHostBoundFailure, HostBoundSample
from roastpilot_agent.cold_characterisation.identity import ColdIdentityFailure
from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    RejectionReason,
)
from roastpilot_agent.mcp_client import FirstCrackStatus
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict


def _audio_payload() -> dict[str, evidence.ColdJsonValue]:
    """Return one complete strict synthetic tick-audio payload."""
    return {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "allow_manual_override": False,
        "reason": None,
        "audio_running": True,
        "queued_window_count": 1,
        "emitted_window_count": 1,
        "dropped_window_count": 0,
        "processed_window_count": 1,
        "mic_peak_dbfs": -3.0,
        "mic_rms_dbfs": -12.0,
        "overflow_count_last_minute": 0,
        "estimated_lost_audio_ms_last_minute": 0.0,
        "total_overflow_count": 0,
        "max_consecutive_overflow_count": 0,
        "last_inference_duration_ms": 2.0,
        "max_inference_duration_ms": 3.0,
        "inference_overrun_count": 0,
    }


def _canonical(payload: object) -> str:
    """Encode synthetic canonical envelope content."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _envelope(kind: evidence.ColdEnvelopeKind) -> evidence.ColdSealedEnvelope:
    """Return a valid synthetic sealed envelope."""
    canonical = _canonical({"a": [1, True], "b": "value"})
    return evidence.ColdSealedEnvelope(
        kind=kind,
        schema_version=1,
        canonical_json=canonical,
        canonical_byte_length=len(canonical.encode("utf-8")),
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _common(stream: str) -> dict[str, typing.Any]:
    """Return the closed common record fields for one stream literal."""
    return {
        "schema_version": 1,
        "stream": stream,
        "run_id": "20260904T143036Z-d183-char-fan-music-retry4",
        "phase": evidence.ColdPhaseKind.RECORDING_ON,
        "recorded_at_utc": "2026-09-04T14:30:36Z",
        "monotonic_seconds": 1.0,
        "identity_sha256": "a" * 64,
    }


def _tick() -> evidence.ColdTickRecord:
    """Return a valid synthetic tick record."""
    return evidence.ColdTickRecord(
        **_common("tick"),
        tick=0,
        bean_temp_c=None,
        env_temp_c=None,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
        connected=True,
        audio=evidence.project_tick_audio(_audio_payload()).audio,
    )


def test_projection_requires_every_declared_audio_field() -> None:
    """A payload lacking any required counter or ordinary field is refused."""
    for name in evidence.ColdTickAudioSample.model_fields:
        payload = _audio_payload()
        del payload[name]
        with pytest.raises(evidence.ColdEvidenceError) as raised:
            evidence.project_tick_audio(payload)
        assert raised.value.failure is evidence.ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT


def test_projection_is_strict_and_preserves_unknown_keys_losslessly() -> None:
    """Known values reject coercion while compatible unknown JSON survives intact."""
    payload = _audio_payload()
    payload["future_counter"] = {"list": [1, "two"]}
    projection = evidence.project_tick_audio(payload)
    assert projection.raw_audio_extra == {"future_counter": {"list": [1, "two"]}}
    payload["queued_window_count"] = "1"
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.project_tick_audio(payload)
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickAudioSample.model_validate({**_audio_payload(), "extra": 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_walker_rejects_nonfinite_values(value: float) -> None:
    """The pre-serialisation walker rejects every non-finite float."""
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.walk_json_value({"value": value})
    assert raised.value.failure is evidence.ColdEvidenceFailure.JSON_VALUE_NOT_FINITE


def test_walker_is_iterative_and_refuses_non_exact_containers() -> None:
    """Deep input avoids recursion and subclasses never become admitted JSON."""
    nested: evidence.ColdJsonValue = None
    for _ in range(evidence.MAX_JSON_DEPTH + 1):
        nested = [nested]
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.walk_json_value(nested)
    assert raised.value.failure is evidence.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED

    class MappingSubclass(dict[str, object]):
        """A hostile mapping-shaped object for exact-type admission testing."""

    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, MappingSubclass()))


@pytest.mark.parametrize(
    ("value", "failure"),
    [
        ({1: "value"}, evidence.ColdEvidenceFailure.JSON_KEY_INVALID),
        (
            {"x" * (evidence.MAX_JSON_KEY_BYTES + 1): 1},
            evidence.ColdEvidenceFailure.JSON_KEY_INVALID,
        ),
        (10**evidence.MAX_INT_DIGITS, evidence.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED),
        (
            [None] * (evidence.MAX_COLLECTION_LENGTH + 1),
            evidence.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED,
        ),
        (
            {str(index): None for index in range(evidence.MAX_COLLECTION_LENGTH + 1)},
            evidence.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED,
        ),
        (object(), evidence.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED),
    ],
)
def test_walker_refuses_each_closed_structural_breach(
    value: object, failure: evidence.ColdEvidenceFailure
) -> None:
    """Every non-admitted structural input maps to a closed failure."""
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, value))
    assert raised.value.failure is failure


def test_walker_enforces_aggregate_text_and_pending_node_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregate bytes and child reservations fail before a hostile push grows the stack."""
    monkeypatch.setattr(evidence, "MAX_INPUT_AGGREGATE_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value("aa")
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value("é")
    monkeypatch.setattr(evidence, "MAX_INPUT_AGGREGATE_BYTES", 524_288)
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", 0)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value(None)
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", 1)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value([None])
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value({"key": None})
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", 4096)
    payload = _audio_payload()
    payload["reason"] = "x" * (evidence.MAX_TEXT_FIELD_BYTES + 1)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.project_tick_audio(payload)


def test_envelope_binds_length_digest_and_canonical_json() -> None:
    """Envelope mutations fail closed without retaining parsed input detail."""
    envelope = _envelope(evidence.ColdEnvelopeKind.IDENTITY)
    assert envelope.sha256 == hashlib.sha256(envelope.canonical_json.encode("utf-8")).hexdigest()
    for changed in (
        {"sha256": "b" * 64},
        {"canonical_byte_length": 0},
        {"canonical_json": '{"b":"value","a":[1,true]}'},
        {"canonical_json": "not json"},
    ):
        with pytest.raises((evidence.ColdEvidenceError, pydantic.ValidationError)):
            typing.cast(typing.Any, envelope.__class__)(**(envelope.model_dump() | changed))


def test_envelope_and_record_kind_pairing_fail_closed() -> None:
    """Envelope caps and stream-specific kinds cannot be bypassed by construction."""
    envelope = _envelope(evidence.ColdEnvelopeKind.IDENTITY)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.ColdFinalisationRecord(
            **_common("finalisation"),
            session_id="session",
            envelope=envelope,
            status=evidence.ColdFinalisationStatus.CLEAN,
            clean=True,
            observed_command_streaming_required=True,
            applied_branch=evidence.ColdCapabilityBranch.STREAMING,
        )
    assert raised.value.failure is evidence.ColdEvidenceFailure.ENVELOPE_KIND_MISMATCHED
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.ColdRunHeader(
            **_common("header"), identity=_envelope(evidence.ColdEnvelopeKind.FINALISATION)
        )
    assert raised.value.failure is evidence.ColdEvidenceFailure.ENVELOPE_KIND_MISMATCHED


def test_envelope_nonfinite_and_cap_paths_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope JSON is bounded before parsing and rejects non-finite representations."""
    canonical = "NaN"
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.ColdSealedEnvelope(
            kind=evidence.ColdEnvelopeKind.IDENTITY,
            schema_version=1,
            canonical_json=canonical,
            canonical_byte_length=len(canonical),
            sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        )
    assert raised.value.failure is evidence.ColdEvidenceFailure.JSON_VALUE_NOT_FINITE
    monkeypatch.setattr(evidence, "MAX_ENVELOPE_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        _envelope(evidence.ColdEnvelopeKind.IDENTITY)
    assert raised.value.failure is evidence.ColdEvidenceFailure.ENVELOPE_TOO_LARGE


def test_designated_ingresses_strip_parser_context_and_attacker_key() -> None:
    """Projection errors retain only closed diagnostic names and no exception chain."""
    payload = _audio_payload()
    payload["attacker=shaped-key"] = typing.cast(evidence.ColdJsonValue, object())
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.project_tick_audio(payload)
    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    assert error.field_names == ()
    assert "attacker" not in repr(error)


def test_validate_record_revalidates_constructed_nested_data_and_copies_maps() -> None:
    """The persistence boundary rejects bypassed nested data and returns snapshots."""
    tick = _tick()
    assert evidence.validate_record(tick) == tick
    raw = {"vendor": [1]}
    constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"raw_vendor_data": raw})
    )
    validated = typing.cast(
        evidence.ColdTickRecord,
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, constructed)),
    )
    raw["vendor"].append(2)
    assert validated.raw_vendor_data == {"vendor": [1]}

    unsafe_evaluation = typing.cast(typing.Any, evidence.ColdSafetyEvaluation).model_construct(
        rule="all_clear",
        verdict=evidence.ColdSafetyVerdict.ALLOW,
        input_heat=0,
        input_fan=0,
        adjusted_heat=101,
        adjusted_fan=0,
        reason="safe",
    )
    advisory = typing.cast(typing.Any, evidence.ColdAdvisoryRecord).model_construct(
        **_common("advisory"),
        requested_heat=0,
        requested_fan=0,
        should_drop=False,
        confidence=0.5,
        latency_seconds=0.1,
        evaluation=unsafe_evaluation,
        failure=None,
    )
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, advisory))


def test_validate_record_bounds_constructed_cycles_and_unknown_shapes_before_adapter() -> None:
    """Constructed cycles and foreign graph nodes refuse before Pydantic serialisation."""
    tick = _tick()
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"raw_vendor_data": cyclic})
    )
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, constructed))
    assert raised.value.failure is evidence.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED

    class Foreign:
        """An inert foreign value which must not be stringified or adapted."""

    foreign = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"audio": Foreign()})
    )
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, foreign))
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_NOT_VALIDATED


def test_validate_record_rejects_foreign_instance_and_record_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict persistence ingress rejects foreign records and every byte-cap breach."""
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, object()))
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_NOT_VALIDATED

    tick = _tick()
    monkeypatch.setattr(evidence, "MAX_RECORD_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(tick)
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_TOO_LARGE


def test_record_union_has_six_closed_streams_and_abort_pairs() -> None:
    """The record vocabulary is six streams and abort reasons remain domain-typed."""
    records: list[evidence.ColdEvidenceRecord] = [
        evidence.ColdRunHeader(
            **_common("header"), identity=_envelope(evidence.ColdEnvelopeKind.IDENTITY)
        ),
        _tick(),
        evidence.ColdHostRecord(
            **_common("host"),
            sample=evidence.ColdHostSample(
                captured_at_utc="2026-09-04T14:30:36Z",
                monotonic_seconds=1.0,
                soc_temp_c=40.0,
                throttled_word_hex="0x0",
                mem_available_bytes=1,
                free_bytes=1,
            ),
        ),
        evidence.ColdAdvisoryRecord(
            **_common("advisory"),
            requested_heat=0,
            requested_fan=0,
            should_drop=False,
            confidence=0.5,
            latency_seconds=0.1,
            evaluation=evidence.ColdSafetyEvaluation(
                rule="all_clear",
                verdict=evidence.ColdSafetyVerdict.ALLOW,
                input_heat=0,
                input_fan=0,
                adjusted_heat=0,
                adjusted_fan=0,
                reason="safe",
            ),
            failure=None,
        ),
        evidence.ColdFinalisationRecord(
            **_common("finalisation"),
            session_id="session",
            envelope=_envelope(evidence.ColdEnvelopeKind.FINALISATION),
            status=evidence.ColdFinalisationStatus.CLEAN,
            clean=True,
            observed_command_streaming_required=True,
            applied_branch=evidence.ColdCapabilityBranch.STREAMING,
        ),
        evidence.ColdAbortRecord(
            **_common("abort"),
            domain=evidence.ColdAbortDomain.OPERATOR,
            reason=evidence.ColdOperatorAbortReason.OPERATOR_STOP,
        ),
    ]
    assert [record.stream for record in records] == [
        member.value for member in evidence.ColdEvidenceStream
    ]
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.ColdAbortRecord(
            **_common("abort"),
            domain=evidence.ColdAbortDomain.HOST,
            reason=evidence.ColdOperatorAbortReason.OPERATOR_STOP,
        )
    assert raised.value.failure is evidence.ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED


def test_consumer_owned_contracts_match_delivered_models_without_importing_them() -> None:
    """Cheap duplicated schema surfaces stay structurally tied to their upstream contracts."""
    assert tuple(evidence.ColdTickAudioSample.model_fields) == tuple(
        FinalisationFirstCrackStatus.model_fields
    )
    assert tuple(evidence.ColdHostSample.model_fields) == tuple(HostBoundSample.model_fields)
    assert tuple(evidence.ColdSafetyEvaluation.model_fields) == tuple(SafetyEvaluation.model_fields)
    assert {member.value for member in evidence.ColdSafetyVerdict} == {
        member.value for member in SafetyVerdict
    }
    assert {member.value for member in evidence.ColdHostAbortReason} == {
        member.value for member in ColdHostBoundFailure
    }
    assert {member.value for member in evidence.ColdIdentityAbortReason} == {
        member.value for member in ColdIdentityFailure
    }
    assert {member.value for member in evidence.ColdMcpAbortReason} == {
        *(member.value for member in RejectionReason),
        "emergency_stop",
        "session_or_reservation_changed",
    }
    assert FirstCrackStatus.model_fields["queued_window_count"].default == 0
    assert "max_consecutive_overflow_count" not in FirstCrackStatus.model_fields


def test_every_closed_enum_member_and_record_stream_is_admissible() -> None:
    """Closed vocabularies enumerate the entire declared grammar, not a subset."""
    assert len(evidence.ColdEvidenceFailure) == 19
    assert len(evidence.ColdAudioField) == 21
    assert len(evidence.ColdMcpAbortReason) == 14
    assert len(evidence.ColdHostAbortReason) == 15
    assert len(evidence.ColdIdentityAbortReason) == 11
    assert len(evidence.ColdSafetyVerdict) == 6
    assert len(evidence.ColdFinalisationStatus) == 6
    assert len(evidence.ColdEvidenceStream) == 6
    for domain, enum_type in (
        (evidence.ColdAbortDomain.HOST, evidence.ColdHostAbortReason),
        (evidence.ColdAbortDomain.IDENTITY, evidence.ColdIdentityAbortReason),
        (evidence.ColdAbortDomain.EVIDENCE, evidence.ColdEvidenceFailure),
        (evidence.ColdAbortDomain.MCP, evidence.ColdMcpAbortReason),
        (evidence.ColdAbortDomain.ADVISOR, evidence.ColdAdvisorFailureKind),
        (evidence.ColdAbortDomain.OPERATOR, evidence.ColdOperatorAbortReason),
    ):
        for reason in enum_type:
            record = evidence.ColdAbortRecord(**_common("abort"), domain=domain, reason=reason)
            assert evidence.validate_record(record) == record


@pytest.mark.parametrize("run_id", ["../escape", "a/b", ".", "..", "", "UPPER", "x ", "x\\y"])
def test_common_identity_fields_are_absolute_and_version_one_only(run_id: str) -> None:
    """Every common record refuses path-shaped ids, uppercase digests, and later schemas."""
    values = _common("tick")
    values["run_id"] = run_id
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickRecord(
            **values,
            tick=0,
            bean_temp_c=None,
            env_temp_c=None,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            connected=True,
            audio=evidence.project_tick_audio(_audio_payload()).audio,
        )
    values = _common("tick")
    values["schema_version"] = 2
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickRecord(
            **values,
            tick=0,
            bean_temp_c=None,
            env_temp_c=None,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            connected=True,
            audio=evidence.project_tick_audio(_audio_payload()).audio,
        )


def test_scope_and_import_fence_are_closed() -> None:
    """The source surface exposes only the two boundary callables publicly."""
    source = pathlib.Path(inspect.getfile(evidence)).read_text()
    tree = ast.parse(source)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {"collections", "enum", "hashlib", "json", "math", "typing", "pydantic"}
    assert not math.isnan(0.0)
    public_functions = {
        name
        for name, value in vars(evidence).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert public_functions == {"walk_json_value", "project_tick_audio", "validate_record"}
    assert not any(
        name in vars(evidence)
        for name in {
            "MAX_CONSECUTIVE_OVERFLOW_N",
            "PEAK_TRAILING_LOST_AUDIO_MS_X",
            "PRODUCTION_FATAL_STREAK",
            "EFFECTIVE_HOP_SECONDS",
        }
    )
