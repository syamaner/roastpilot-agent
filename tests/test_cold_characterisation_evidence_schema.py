"""Behavioural tests for the closed cold evidence schema boundary."""

import ast
import hashlib
import inspect
import json
import math
import pathlib
import sys
import typing
import warnings

import pydantic
import pytest

from roastpilot_agent.advisor import AdvisorDescriptor
from roastpilot_agent.cold_characterisation import evidence_schema as evidence
from roastpilot_agent.cold_characterisation.host import ColdHostBoundFailure, HostBoundSample
from roastpilot_agent.cold_characterisation.identity import (
    REQUIRED_MCP_VERSION,
    AgentBuildProvenance,
    ColdArtefactKind,
    ColdIdentityFailure,
    EffectiveMCPProfile,
    freeze_identity,
    identity_sha256,
)
from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    RejectionReason,
)
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import FirstCrackStatus, RuntimeConfigSnapshot, ServerInfo
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
    payload = typing.cast(dict[str, typing.Any], _audio_payload())
    payload["future_counter"] = {"list": [1, "two"]}
    projection = evidence.project_tick_audio(payload)
    assert projection.raw_audio_extra == {"future_counter": {"list": [1, "two"]}}
    payload["queued_window_count"] = "1"
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.project_tick_audio(payload)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, object()))
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.project_tick_audio(typing.cast(dict[str, evidence.ColdJsonValue], []))
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickAudioSample.model_validate({**_audio_payload(), "extra": 1})
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickAudioSample.model_validate(
            {**_audio_payload(), "queued_window_count": "1", "allow_manual_override": 1}
        )
    payload = typing.cast(dict[str, typing.Any], _audio_payload())
    nested = {"list": [1]}
    payload["future"] = nested
    projected = evidence.project_tick_audio(payload)
    nested["list"].append(2)
    assert projected.raw_audio_extra == {"future": {"list": [1]}}
    returned_future = typing.cast(dict[str, list[int]], projected.raw_audio_extra["future"])
    returned_future["list"].append(3)
    assert nested == {"list": [1, 2]}


def test_configs_and_rust_patterns_preserve_the_ratified_absolute_grammar() -> None:
    """Schema configs match the strict mirror and Rust patterns keep absolute ends."""
    from roastpilot_agent.cold_characterisation.mcp import StrictMCPMirror

    model_config = getattr(evidence, "_" + "COLD_EVIDENCE_MODEL_CONFIG")
    strict_config = getattr(evidence, "_" + "COLD_EVIDENCE_STRICT_CONFIG")
    run_pattern = getattr(evidence, "_" + "RUN_ID_PATTERN")
    run_rust_pattern = getattr(evidence, "_" + "RUN_ID_RUST_PATTERN")
    sha_pattern = getattr(evidence, "_" + "SHA256_PATTERN")
    sha_rust_pattern = getattr(evidence, "_" + "SHA256_RUST_PATTERN")
    assert model_config == {
        "frozen": True,
        "extra": "forbid",
        "allow_inf_nan": False,
    }
    assert StrictMCPMirror.model_config == strict_config
    assert run_pattern.endswith(r"\Z")
    assert run_rust_pattern.endswith(r"\z")
    assert sha_pattern.endswith(r"\Z")
    assert sha_rust_pattern.endswith(r"\z")
    payload = _common("tick")
    payload["run_id"] += "\n"
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdTickRecord(
            **payload,
            tick=0,
            bean_temp_c=None,
            env_temp_c=None,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            connected=True,
            audio=evidence.project_tick_audio(_audio_payload()).audio,
        )


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
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value(None)
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
    with pytest.raises(pydantic.ValidationError):
        evidence.ColdSafetyEvaluation(
            rule="all_clear",
            verdict=evidence.ColdSafetyVerdict.ALLOW,
            input_heat=0,
            input_fan=0,
            adjusted_heat=0,
            adjusted_fan=0,
            reason="x" * (evidence.MAX_TEXT_FIELD_BYTES + 1),
        )


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


def test_projection_and_envelope_close_byte_and_unexpected_exception_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map and envelope byte-cap failures use fixed closed errors."""
    payload = _audio_payload()
    payload["future"] = "x"
    monkeypatch.setattr(evidence, "MAX_RAW_AUDIO_EXTRA_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.project_tick_audio(payload)
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE

    constructed = evidence.ColdSealedEnvelope.model_construct(
        kind=evidence.ColdEnvelopeKind.IDENTITY,
        schema_version=1,
        canonical_json="\ud800",
        canonical_byte_length=1,
        sha256="a" * 64,
    )
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        typing.cast(
            typing.Callable[[], object], getattr(constructed, "_" + "validate_canonical_bytes")
        )()
    assert raised.value.failure is evidence.ColdEvidenceFailure.ENVELOPE_NOT_CANONICAL


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


def test_envelope_rejects_noncanonical_valid_json_after_digest_check() -> None:
    """Canonicality is independently checked after the matching digest is admitted."""
    canonical = '{"b":1,"a":2}'
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.ColdSealedEnvelope(
            kind=evidence.ColdEnvelopeKind.IDENTITY,
            schema_version=1,
            canonical_json=canonical,
            canonical_byte_length=len(canonical),
            sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        )
    assert raised.value.failure is evidence.ColdEvidenceFailure.ENVELOPE_NOT_CANONICAL


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


def test_raw_extraction_covers_closed_graph_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw extraction copies every admitted scalar/container shape before the adapter."""
    tick = _tick().model_copy(update={"raw_vendor_data": {"nested": [None, True, 1, 1.5, "text"]}})
    private_name = "_" + "extract_model"
    extract = getattr(evidence, private_name)
    extracted = extract(tick, [0])
    assert extracted["raw_vendor_data"] == {"nested": [None, True, 1, 1.5, "text"]}
    monkeypatch.setattr(evidence, "MAX_TEXT_FIELD_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        extract(tick, [0])
    assert raised.value.failure is evidence.ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE
    monkeypatch.setattr(evidence, "MAX_TEXT_FIELD_BYTES", 2_048)

    raw_reason = typing.cast(typing.Any, evidence.ColdAbortRecord).model_construct(
        **_common("abort"),
        domain=evidence.ColdAbortDomain.OPERATOR,
        reason="operator_stop",
    )
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, raw_reason))
    assert raised.value.failure is evidence.ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED


def test_raw_extraction_refuses_all_constructed_shape_and_budget_breaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every hostile constructed graph shape is refused before strict adaptation."""
    tick = _tick()
    extract = getattr(evidence, "_" + "extract_model")

    missing = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(**tick.model_dump())
    object.__getattribute__(missing, "__dict__").pop("tick")
    with pytest.raises(evidence.ColdEvidenceError):
        extract(missing, [0])

    absent = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(**tick.model_dump())
    absent_data = object.__getattribute__(absent, "__dict__")
    absent_data.pop("tick")
    absent_data["unexpected"] = 0
    with pytest.raises(evidence.ColdEvidenceError):
        extract(absent, [0])

    unexpected = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **tick.model_dump()
    )
    object.__getattribute__(unexpected, "__dict__")["unexpected"] = 1
    with pytest.raises(evidence.ColdEvidenceError):
        extract(unexpected, [0])

    wrong_name = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **tick.model_dump()
    )
    wrong_data = object.__getattribute__(wrong_name, "__dict__")
    wrong_data.pop("tick")
    wrong_data[type("Key", (str,), {})("tick")] = 0
    with pytest.raises(evidence.ColdEvidenceError):
        extract(wrong_name, [0])

    wrong_dict = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **tick.model_dump()
    )
    object.__setattr__(wrong_dict, "__pydantic_extra__", [])
    with pytest.raises(evidence.ColdEvidenceError):
        extract(wrong_dict, [0])

    extra = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(**tick.model_dump())
    object.__setattr__(extra, "__pydantic_extra__", {"unexpected": 1})
    with pytest.raises(evidence.ColdEvidenceError):
        extract(extra, [0])

    for hostile in (
        {str(index): None for index in range(evidence.MAX_COLLECTION_LENGTH + 1)},
        {"vendor": [None] * (evidence.MAX_COLLECTION_LENGTH + 1)},
        {"vendor": {"x": [None] * (evidence.MAX_COLLECTION_LENGTH + 1)}},
        {"vendor": {1: "bad"}},
        {"vendor": {"x" * (evidence.MAX_JSON_KEY_BYTES + 1): 1}},
        {"vendor": 10**evidence.MAX_INT_DIGITS},
        {"vendor": float("nan")},
    ):
        constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
            **(tick.model_dump() | {"raw_vendor_data": hostile})
        )
        with pytest.raises(evidence.ColdEvidenceError):
            extract(constructed, [0])

    deep: object = []
    for _ in range(evidence.MAX_JSON_DEPTH + 1):
        deep = [deep]
    constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"raw_vendor_data": {"deep": deep}})
    )
    with pytest.raises(evidence.ColdEvidenceError):
        extract(constructed, [0])

    class MappingSubclass(dict[str, object]):
        """Hostile non-exact mapping for raw extraction."""

    for hostile in (MappingSubclass(), object()):
        constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
            **(tick.model_dump() | {"raw_vendor_data": hostile})
        )
        with pytest.raises(evidence.ColdEvidenceError):
            extract(constructed, [0])

    monkeypatch.setattr(evidence, "MAX_INPUT_AGGREGATE_BYTES", 10)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        extract(tick, [0])
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_TOO_LARGE
    monkeypatch.setattr(evidence, "MAX_INPUT_AGGREGATE_BYTES", 524_288)
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", 0)
    with pytest.raises(evidence.ColdEvidenceError):
        extract(tick, [0])
    root_aggregate = [0]
    root_fields = getattr(evidence, "_" + "extract_model_fields")(tick, root_aggregate)
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", len(root_fields) + 21)
    map_record = tick.model_copy(update={"raw_vendor_data": {"one": 1}})
    with pytest.raises(evidence.ColdEvidenceError):
        extract(map_record, [0])
    list_record = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"raw_vendor_data": [1]})
    )
    with pytest.raises(evidence.ColdEvidenceError):
        extract(list_record, [0])

    host = evidence.ColdHostRecord(
        **_common("host"),
        sample=evidence.ColdHostSample(
            captured_at_utc="now",
            monotonic_seconds=1.0,
            soc_temp_c=1.0,
            throttled_word_hex="0x0",
            mem_available_bytes=1,
            free_bytes=1,
        ),
    )
    host_fields = getattr(evidence, "_" + "extract_model_fields")(host, [0])
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", len(host_fields) + 2)
    with pytest.raises(evidence.ColdEvidenceError):
        extract(host, [0])
    monkeypatch.setattr(evidence, "MAX_JSON_NODES", 4_096)
    monkeypatch.setattr(evidence, "MAX_JSON_DEPTH", 1)
    with pytest.raises(evidence.ColdEvidenceError):
        extract(tick, [0])

    aggregate = [0]
    getattr(evidence, "_" + "extract_model_fields")(tick, aggregate)
    with pytest.raises(evidence.ColdEvidenceError):
        extract(tick, [evidence.MAX_INPUT_AGGREGATE_BYTES - aggregate[0] - 1])


def test_audio_reason_is_not_a_new_schema_owned_text_cap() -> None:
    """Mirrored MCP audio text remains limited only by aggregate and record budgets."""
    payload = _audio_payload()
    payload["reason"] = "x" * (evidence.MAX_TEXT_FIELD_BYTES + 1)
    tick = _tick().model_copy(update={"audio": evidence.project_tick_audio(payload).audio})
    validated = typing.cast(evidence.ColdTickRecord, evidence.validate_record(tick))
    assert validated.audio.reason == payload["reason"]


def test_exact_limits_accept_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every bounded JSON and map surface accepts its exact declared limit."""
    assert evidence.walk_json_value(10**evidence.MAX_INT_DIGITS - 1) is None
    assert evidence.walk_json_value({"x" * evidence.MAX_JSON_KEY_BYTES: None}) is None
    assert (
        evidence.walk_json_value(
            typing.cast(evidence.ColdJsonValue, [None] * evidence.MAX_COLLECTION_LENGTH)
        )
        is None
    )
    nested: evidence.ColdJsonValue = []
    for _ in range(evidence.MAX_JSON_DEPTH):
        nested = [nested]
    assert evidence.walk_json_value(nested) is None

    extra_overhead = len(_canonical({"future": ""}).encode())
    payload = _audio_payload()
    payload["future"] = "x" * (evidence.MAX_RAW_AUDIO_EXTRA_BYTES - extra_overhead)
    projection = evidence.project_tick_audio(payload)
    assert (
        len(_canonical(projection.raw_audio_extra).encode()) == evidence.MAX_RAW_AUDIO_EXTRA_BYTES
    )

    vendor_overhead = len(_canonical({"vendor": ""}).encode())
    tick = _tick().model_copy(
        update={
            "raw_vendor_data": {"vendor": "x" * (evidence.MAX_VENDOR_BLOB_BYTES - vendor_overhead)}
        }
    )
    validated = typing.cast(evidence.ColdTickRecord, evidence.validate_record(tick))
    assert len(_canonical(validated.raw_vendor_data).encode()) == evidence.MAX_VENDOR_BLOB_BYTES

    control_count, tail = divmod(evidence.MAX_ENVELOPE_BYTES - 8, 6)
    canonical = '{"x":"' + ("\\u0001" * control_count) + ("a" * tail) + '"}'
    assert len(canonical.encode()) == evidence.MAX_ENVELOPE_BYTES
    envelope = evidence.ColdSealedEnvelope(
        kind=evidence.ColdEnvelopeKind.IDENTITY,
        schema_version=1,
        canonical_json=canonical,
        canonical_byte_length=evidence.MAX_ENVELOPE_BYTES,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    assert envelope.canonical_byte_length == evidence.MAX_ENVELOPE_BYTES
    assert evidence.walk_json_value("x" * evidence.MAX_INPUT_AGGREGATE_BYTES) is None
    node_limited = [[None, None, None] for _ in range(evidence.MAX_COLLECTION_LENGTH - 1)] + [
        [None, None]
    ]
    assert evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, node_limited)) is None
    text_limited = evidence.ColdSafetyEvaluation(
        rule="r" * evidence.MAX_TEXT_FIELD_BYTES,
        verdict=evidence.ColdSafetyVerdict.REJECT,
        input_heat=None,
        input_fan=None,
        adjusted_heat=None,
        adjusted_fan=None,
        reason="q" * evidence.MAX_TEXT_FIELD_BYTES,
    )
    assert text_limited.adjusted_heat is None and text_limited.adjusted_fan is None

    low, high = 0, evidence.MAX_RECORD_BYTES
    accepted_header: evidence.ColdRunHeader | None = None
    while low <= high:
        middle = (low + high) // 2
        canonical_record = '{"x":"' + ("a" * middle) + '"}'
        candidate = evidence.ColdRunHeader(
            **_common("header"),
            identity=evidence.ColdSealedEnvelope(
                kind=evidence.ColdEnvelopeKind.IDENTITY,
                schema_version=1,
                canonical_json=canonical_record,
                canonical_byte_length=len(canonical_record),
                sha256=hashlib.sha256(canonical_record.encode()).hexdigest(),
            ),
        )
        size = len(_canonical(candidate.model_dump(mode="json")).encode())
        if size <= evidence.MAX_RECORD_BYTES:
            accepted_header, low = candidate, middle + 1
        else:
            high = middle - 1
    assert accepted_header is not None
    assert (
        len(_canonical(accepted_header.model_dump(mode="json")).encode())
        == evidence.MAX_RECORD_BYTES
    )
    assert evidence.validate_record(accepted_header) == accepted_header
    monkeypatch.setattr(evidence, "MAX_INPUT_AGGREGATE_BYTES", 1)
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.walk_json_value(None)


def test_projection_error_locations_are_closed_schema_names_only() -> None:
    """Known, nested, integer, and hostile parser locations never escape diagnostics."""
    projection_fields = getattr(evidence, "_" + "projection_fields")
    with pytest.raises(pydantic.ValidationError) as raised:
        evidence.ColdTickAudioSample.model_validate(
            {**_audio_payload(), "queued_window_count": "bad", "nested": {1: "bad"}}
        )
    fields = projection_fields(raised.value)
    assert fields
    assert all(isinstance(field, evidence.ColdAudioField) for field in fields)
    assert evidence.ColdAudioField.UNKNOWN_FIELD in fields
    assert "nested" not in repr(fields)
    validation_error = pydantic.ValidationError.from_exception_data(
        "synthetic",
        typing.cast(
            typing.Any,
            [
                {"type": "missing", "loc": ("nested", "field"), "input": None},
                {"type": "missing", "loc": (1,), "input": None},
                {"type": "missing", "loc": (object(),), "input": None},
            ],
        ),
    )
    assert projection_fields(validation_error) == (
        evidence.ColdAudioField.UNKNOWN_FIELD,
        evidence.ColdAudioField.UNKNOWN_FIELD,
        evidence.ColdAudioField.UNKNOWN_FIELD,
    )
    bad_payload = _audio_payload()
    bad_payload["queued_window_count"] = "not-an-integer"
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.project_tick_audio(bad_payload)
    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    assert error.args == ("Cold evidence admission failed.",)
    assert error.field_names == (evidence.ColdAudioField.QUEUED_WINDOW_COUNT,)
    assert "not-an-integer" not in repr(error)
    assert not hasattr(error, "errors") and not getattr(error, "__notes__", ())


def test_tolerant_mirror_can_launder_missing_counter_but_projection_refuses() -> None:
    """The delivered tolerant mirror contrast proves why this projection is strict."""
    payload = _audio_payload()
    del payload["max_consecutive_overflow_count"]
    tolerant = FirstCrackStatus.model_validate(payload)
    assert tolerant.status == "pending"
    assert "max_consecutive_overflow_count" not in FirstCrackStatus.model_fields
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.project_tick_audio(payload)


def test_union_advisory_and_record_surfaces_are_closed() -> None:
    """Records expose exactly six streams and no untyped advisory or actuator fields."""
    assert set(evidence.ColdTickRecord.model_fields) == {
        "schema_version",
        "stream",
        "run_id",
        "phase",
        "recorded_at_utc",
        "monotonic_seconds",
        "identity_sha256",
        "tick",
        "bean_temp_c",
        "env_temp_c",
        "heat_level_percent",
        "fan_level_percent",
        "cooling_on",
        "connected",
        "audio",
        "raw_audio_extra",
        "raw_vendor_data",
    }
    assert not {"main_fan", "drum", "solenoid", "verdict"} & set(
        evidence.ColdTickRecord.model_fields
    )
    assert tuple(evidence.ColdEvidenceStream) == (
        evidence.ColdEvidenceStream.HEADER,
        evidence.ColdEvidenceStream.TICK,
        evidence.ColdEvidenceStream.HOST,
        evidence.ColdEvidenceStream.ADVISORY,
        evidence.ColdEvidenceStream.FINALISATION,
        evidence.ColdEvidenceStream.ABORT,
    )
    evaluation = evidence.ColdSafetyEvaluation.model_fields
    assert tuple(evaluation) == (
        "rule",
        "verdict",
        "input_heat",
        "input_fan",
        "adjusted_heat",
        "adjusted_fan",
        "reason",
    )
    assert set(evidence.ColdAdvisorFailureKind) == {
        evidence.ColdAdvisorFailureKind.TIMEOUT,
        evidence.ColdAdvisorFailureKind.PROVIDER_ERROR,
        evidence.ColdAdvisorFailureKind.MALFORMED_OUTPUT,
        evidence.ColdAdvisorFailureKind.UNSAFE_OUTPUT,
    }
    assert not {"message", "body", "prompt", "url", "credential"} & set(
        evidence.ColdAdvisoryRecord.model_fields
    )
    with pytest.raises((pydantic.ValidationError, evidence.ColdEvidenceError)):
        evidence.ColdSafetyEvaluation.model_validate({"rule": "r", "verdict": "allow"})
    for verdict in (evidence.ColdSafetyVerdict.REJECT, evidence.ColdSafetyVerdict.FAULT):
        assert (
            evidence.ColdSafetyEvaluation(
                rule="r",
                verdict=verdict,
                input_heat=None,
                input_fan=None,
                adjusted_heat=None,
                adjusted_fan=None,
                reason="r",
            ).adjusted_fan
            is None
        )


def test_construct_copy_snapshot_and_no_warning_boundary_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid constructed data is revalidated, enums stay typed, and hostile data never warns."""
    tick = _tick()
    constructed = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **tick.model_dump()
    )
    validated = typing.cast(evidence.ColdTickRecord, evidence.validate_record(constructed))
    assert isinstance(validated.phase, evidence.ColdPhaseKind)
    nested_audio = typing.cast(typing.Any, evidence.ColdTickAudioSample).model_construct(
        **_audio_payload()
    )
    nested_valid = tick.model_copy(update={"audio": nested_audio})
    assert isinstance(evidence.validate_record(nested_valid), evidence.ColdTickRecord)
    missing_counter = typing.cast(typing.Any, evidence.ColdTickAudioSample).model_construct(
        **{
            key: value
            for key, value in _audio_payload().items()
            if key != "inference_overrun_count"
        }
    )
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.validate_record(tick.model_copy(update={"audio": missing_counter}))
    copied = tick.model_copy(update={"heat_level_percent": 101})
    with pytest.raises(evidence.ColdEvidenceError):
        evidence.validate_record(copied)
    hostile = typing.cast(typing.Any, evidence.ColdTickRecord).model_construct(
        **(tick.model_dump() | {"raw_vendor_data": {"hostile": object()}})
    )
    dump_calls: list[object] = []
    original_dump = evidence.ColdTickRecord.model_dump

    def dump_spy(
        self: evidence.ColdTickRecord, *args: object, **kwargs: object
    ) -> dict[str, object]:
        dump_calls.append(self)
        return typing.cast(dict[str, object], original_dump(self, *args, **kwargs))

    monkeypatch.setattr(evidence.ColdTickRecord, "model_dump", dump_spy)
    with warnings.catch_warnings(record=True) as caught, pytest.raises(evidence.ColdEvidenceError):
        evidence.validate_record(hostile)
    assert not caught
    assert dump_calls == []


def test_contract_annotations_union_and_all_anchored_consumers_are_exact() -> None:
    """Consumer-owned fields preserve upstream shape and every anchored consumer rejects drift."""
    assert tuple(evidence.ColdTickAudioSample.model_fields) == tuple(
        FinalisationFirstCrackStatus.model_fields
    )
    for name, field in evidence.ColdTickAudioSample.model_fields.items():
        assert field.annotation == FinalisationFirstCrackStatus.model_fields[name].annotation
    for name, field in evidence.ColdHostSample.model_fields.items():
        assert field.annotation == HostBoundSample.model_fields[name].annotation
        assert field.metadata == HostBoundSample.model_fields[name].metadata
    for name in ("rule", "input_heat", "input_fan", "adjusted_heat", "adjusted_fan", "reason"):
        assert (
            evidence.ColdSafetyEvaluation.model_fields[name].annotation
            == SafetyEvaluation.model_fields[name].annotation
        )
        actual_metadata = tuple(
            metadata
            for metadata in evidence.ColdSafetyEvaluation.model_fields[name].metadata
            if type(metadata).__name__ != "MaxLen"
        )
        assert actual_metadata == tuple(SafetyEvaluation.model_fields[name].metadata)
    assert (
        evidence.ColdSafetyEvaluation.model_fields["rule"].metadata[-1].max_length
        == evidence.MAX_TEXT_FIELD_BYTES
    )
    assert (
        evidence.ColdSafetyEvaluation.model_fields["reason"].metadata[-1].max_length
        == evidence.MAX_TEXT_FIELD_BYTES
    )
    assert evidence.ColdSafetyEvaluation.model_fields["adjusted_heat"].metadata[0].ge == 0
    assert evidence.ColdSafetyEvaluation.model_fields["adjusted_heat"].metadata[1].le == 100
    assert evidence.ColdSafetyEvaluation.model_fields["adjusted_fan"].metadata[0].ge == 0
    assert evidence.ColdSafetyEvaluation.model_fields["adjusted_fan"].metadata[1].le == 100
    union = typing.get_args(evidence.ColdEvidenceRecord)[0]
    assert set(typing.get_args(union)) == {
        evidence.ColdRunHeader,
        evidence.ColdTickRecord,
        evidence.ColdHostRecord,
        evidence.ColdAdvisoryRecord,
        evidence.ColdFinalisationRecord,
        evidence.ColdAbortRecord,
    }
    for record_type, stream in zip(
        typing.get_args(union), evidence.ColdEvidenceStream, strict=True
    ):
        assert record_type.model_fields["schema_version"].annotation == typing.Literal[1]
        assert record_type.model_fields["stream"].annotation == typing.Literal[stream.value]
        assert record_type.model_fields["run_id"].metadata[0].pattern == getattr(
            evidence, "_" + "RUN_ID_RUST_PATTERN"
        )
        assert record_type.model_fields["identity_sha256"].metadata[0].pattern == getattr(
            evidence, "_" + "SHA256_RUST_PATTERN"
        )
    assert evidence.ColdAdvisoryRecord.model_fields["failure"].annotation == (
        evidence.ColdAdvisorFailureKind | None
    )
    for digest in ("A" * 64, "g" * 64, ("a" * 64) + "\n"):
        values = _common("tick")
        values["identity_sha256"] = digest
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
    assert evidence.ColdSealedEnvelope.model_fields["sha256"].metadata[0].pattern == getattr(
        evidence, "_" + "SHA256_RUST_PATTERN"
    )


def test_foreign_instance_is_rejected_before_extractor_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact-class guard does not inspect a foreign instance's hostile attributes."""

    class Foreign:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(name)

    calls: list[object] = []

    def extractor_spy(value: object, aggregate: list[int]) -> dict[str, object]:
        calls.append(value)
        raise AssertionError(aggregate)

    monkeypatch.setattr(evidence, "_" + "extract_model", extractor_spy)
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.validate_record(typing.cast(evidence.ColdEvidenceRecord, Foreign()))
    assert raised.value.failure is evidence.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    assert calls == []


def test_identity_envelope_binds_delivered_identity_canonical_digest(
    tmp_path: pathlib.Path,
) -> None:
    """A synthetic identity's retained bytes and delivered digest agree exactly."""
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("123e4567-e89b-12d3-a456-426614174000\n", encoding="ascii")
    runtime = RuntimeConfigSnapshot.model_validate(
        {
            "config_source": None,
            "roaster_driver": "hottop_kn8828b_2k_plus",
            "roaster_port": "/dev/ttyUSB0",
            "roaster_baudrate": 115200,
            "temperature_unit": "celsius",
            "command_interval_seconds": 0.3,
            "first_crack_mode": "audio",
            "model_repo_id": "repo",
            "model_precision": "int8",
            "allow_manual_override": False,
            "log_dir": "logs",
            "sample_interval_seconds": 5.0,
            "auto_t0_detection_enabled": False,
            "auto_t0_drop_threshold_c": 25.0,
        }
    )
    server = ServerInfo(
        product_name="Coffee Roaster MCP",
        package_name="coffee-roaster-mcp",
        version="0.2.1",
        transport="stdio",
        current_phase="bootstrap",
        roaster_driver="hottop_kn8828b_2k_plus",
        first_crack_mode="audio",
        bootstrap_safe=True,
        available_bootstrap_tools=(),
        started_at_utc="2026-09-22T00:00:00Z",
    )
    provenance = AgentBuildProvenance(
        source_revision="b" * 40,
        source_tree_dirty=False,
        artefact_kind=ColdArtefactKind.WHEEL,
        artefact_sha256="a" * 64,
    )
    profile = EffectiveMCPProfile(
        source_sha256="a" * 64,
        source_byte_length=100,
        first_crack_onnx_threads=2,
        first_crack_min_positive_windows=3,
        first_crack_confirmation_window_seconds=30.0,
        first_crack_revision="revision",
        audio_sample_rate=16000,
        audio_window_seconds=10.0,
        audio_overlap=0.3,
        audio_hop_seconds=None,
        session_ror_window_seconds=60,
        session_ror_min_sample_seconds=10,
    )
    identity = freeze_identity(
        run_id="cold-1",
        started_at_utc="2026-09-22T00:00:00Z",
        coffee_roaster_mcp_version=REQUIRED_MCP_VERSION,
        python_version="3.11.9",
        platform="linux",
        machine="aarch64",
        operating_system="Linux",
        kernel="6.6.0",
        pi_model="Raspberry Pi 5",
        pi_revision="d04170",
        runtime_config=runtime,
        server_info=server,
        device_config=MCPDeviceConfig(recording_devices=("USB microphone",)),
        build_provenance=provenance,
        effective_mcp_profile=profile,
        audio_device_identity="USB microphone",
        serial_port_path="/dev/ttyUSB0",
        controller_tick_seconds=1.0,
        pi_evidence_root="/synthetic/pi",
        laptop_evidence_root="/synthetic/laptop",
        advisor_descriptor=AdvisorDescriptor(
            provider="openrouter", model="test/model", prompt_version="v1"
        ),
        credential_env_var_name="OPENROUTER_API_KEY",
        credential_present=True,
        stimulus_block="tap",
        operator_host_notes="host",
        operator_psu_notes="psu",
        operator_cooling_notes="cooling",
        boot_id_path=boot_id,
    )
    with warnings.catch_warnings(record=True) as caught:
        canonical = _canonical(identity.model_dump(mode="json"))
        digest = identity_sha256(identity)
    assert not caught
    envelope = evidence.ColdSealedEnvelope(
        kind=evidence.ColdEnvelopeKind.IDENTITY,
        schema_version=1,
        canonical_json=canonical,
        canonical_byte_length=len(canonical.encode()),
        sha256=digest,
    )
    assert envelope.sha256 == digest


def test_persisted_raw_empty_container_at_exact_depth_is_admitted() -> None:
    """An empty raw container at the maximum node depth adds no prohibited child."""
    nested: evidence.ColdJsonValue = []
    for _ in range(evidence.MAX_JSON_DEPTH - 2):
        nested = [nested]
    tick = _tick().model_copy(update={"raw_vendor_data": {"nested": nested}})
    validated = typing.cast(evidence.ColdTickRecord, evidence.validate_record(tick))
    assert validated.raw_vendor_data == {"nested": nested}


def test_walker_closes_unencodable_unicode_without_exception_chain() -> None:
    """Malformed UTF-8 source text cannot leak a Unicode encoder exception."""
    for value in ("\ud800", {"\ud800": 1}):
        with pytest.raises(evidence.ColdEvidenceError) as raised:
            evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, value))
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


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
    expected_fields = {
        evidence.ColdRunHeader: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "identity",
        ),
        evidence.ColdTickRecord: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "tick",
            "bean_temp_c",
            "env_temp_c",
            "heat_level_percent",
            "fan_level_percent",
            "cooling_on",
            "connected",
            "audio",
            "raw_audio_extra",
            "raw_vendor_data",
        ),
        evidence.ColdHostRecord: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "sample",
        ),
        evidence.ColdAdvisoryRecord: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "requested_heat",
            "requested_fan",
            "should_drop",
            "confidence",
            "latency_seconds",
            "evaluation",
            "failure",
        ),
        evidence.ColdFinalisationRecord: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "session_id",
            "envelope",
            "status",
            "clean",
            "observed_command_streaming_required",
            "applied_branch",
        ),
        evidence.ColdAbortRecord: (
            "schema_version",
            "stream",
            "run_id",
            "phase",
            "recorded_at_utc",
            "monotonic_seconds",
            "identity_sha256",
            "domain",
            "reason",
        ),
    }
    assert {type(record) for record in records} == set(expected_fields)
    for record_type, names in expected_fields.items():
        assert tuple(record_type.model_fields) == names
    for digest in ("A" * 64, "g" * 64, ("a" * 64) + "\n"):
        for record in records:
            with pytest.raises(pydantic.ValidationError):
                typing.cast(typing.Any, type(record))(
                    **(record.model_dump() | {"identity_sha256": digest})
                )
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
    for enum_type in (
        evidence.ColdAudioField,
        evidence.ColdEvidenceFailure,
        evidence.ColdPhaseKind,
        evidence.ColdEvidenceStream,
        evidence.ColdEnvelopeKind,
        evidence.ColdCapabilityBranch,
        evidence.ColdFinalisationStatus,
        evidence.ColdAdvisorFailureKind,
        evidence.ColdAbortDomain,
        evidence.ColdOperatorAbortReason,
    ):
        assert tuple((member.name, member.value) for member in enum_type) == tuple(
            (member.name, member.name.lower()) for member in enum_type
        )
    assert tuple(member.value for member in evidence.ColdPhaseKind) == (
        "recording_off",
        "recording_on",
    )
    assert tuple(member.value for member in evidence.ColdEvidenceStream) == (
        "header",
        "tick",
        "host",
        "advisory",
        "finalisation",
        "abort",
    )
    assert tuple(member.value for member in evidence.ColdEnvelopeKind) == (
        "identity",
        "finalisation",
    )
    assert tuple(member.value for member in evidence.ColdCapabilityBranch) == (
        "streaming",
        "non_streaming",
    )
    assert tuple(member.value for member in evidence.ColdFinalisationStatus) == (
        "rejected",
        "clean",
        "completed_not_clean",
        "partial",
        "disconnect_indeterminate",
        "aborted",
    )
    assert tuple(member.value for member in evidence.ColdAdvisorFailureKind) == (
        "timeout",
        "provider_error",
        "malformed_output",
        "unsafe_output",
    )
    assert tuple(member.value for member in evidence.ColdAbortDomain) == (
        "host",
        "identity",
        "evidence",
        "mcp",
        "advisor",
        "operator",
    )
    assert tuple(member.value for member in evidence.ColdOperatorAbortReason) == ("operator_stop",)
    assert all(field.is_required() for field in evidence.ColdTickAudioSample.model_fields.values())
    for model in (
        evidence.ColdTickAudioSample,
        evidence.ColdHostSample,
        evidence.ColdSafetyEvaluation,
    ):
        assert model.model_config == getattr(evidence, "_" + "COLD_EVIDENCE_STRICT_CONFIG")
    for model in (
        evidence.ColdTickProjection,
        evidence.ColdSealedEnvelope,
        evidence.ColdRunHeader,
        evidence.ColdTickRecord,
        evidence.ColdHostRecord,
        evidence.ColdAdvisoryRecord,
        evidence.ColdFinalisationRecord,
        evidence.ColdAbortRecord,
    ):
        assert model.model_config == getattr(evidence, "_" + "COLD_EVIDENCE_MODEL_CONFIG")
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
    """The source surface has only approved imports, callables, and no verdict policy."""
    source = pathlib.Path(inspect.getfile(evidence)).read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {"collections.abc", "enum", "hashlib", "json", "math", "typing", "pydantic"}
    assert imported_from == set()
    dynamic_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        )
    ]
    assert dynamic_imports == []
    assert not math.isnan(0.0)
    public_callables = {
        name
        for name, value in vars(evidence).items()
        if not name.startswith("_")
        and callable(value)
        and not inspect.isclass(value)
        and name not in {"ColdJsonValue", "ColdEvidenceRecord"}
    }
    assert public_callables == {"walk_json_value", "project_tick_audio", "validate_record"}
    assert not any(
        name in vars(evidence)
        for name in {
            "MAX_CONSECUTIVE_OVERFLOW_N",
            "PEAK_TRAILING_LOST_AUDIO_MS_X",
            "PRODUCTION_FATAL_STREAK",
            "EFFECTIVE_HOP_SECONDS",
            "HOST_MAX_TEMP",
            "HOST_MIN_",
        }
    )


def test_walker_is_structurally_iterative_beyond_python_recursion_depth() -> None:
    """The walker has no self-call and refuses a very deep graph structurally, not recursively."""
    function = next(
        node
        for node in ast.walk(ast.parse(pathlib.Path(inspect.getfile(evidence)).read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "_walk_json_value"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function.name
        for node in ast.walk(function)
    )
    assert any(isinstance(node, ast.While) for node in ast.walk(function))
    assert any(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "stack"
        for node in ast.walk(function)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_copy_json_value"
        for node in ast.walk(function)
    )
    deep: object = None
    for _ in range(sys.getrecursionlimit() + 10):
        deep = [deep]
    with pytest.raises(evidence.ColdEvidenceError) as raised:
        evidence.walk_json_value(typing.cast(evidence.ColdJsonValue, deep))
    assert raised.value.failure is evidence.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED
