"""Behavioural tests for contained cold evidence construction, plus shared helpers."""

import ast
import enum
import hashlib
import inspect
import json
import math
import re
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.advisor import AdvisorDescriptor
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import (
    REQUIRED_MCP_VERSION,
    AgentBuildProvenance,
    ColdArtefactKind,
    ColdRunIdentity,
    EffectiveMCPProfile,
    freeze_identity,
    identity_sha256,
)
from roastpilot_agent.cold_characterisation.mcp import (
    ColdRoastFanOutcome,
    ColdTickDeviceState,
    ColdTickObservation,
    ColdTickRoastFanObservation,
    ColdTickSessionMetadata,
    SessionFinalisationResult,
)
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import (
    MCPPhase,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerInfo,
)

RUN_ID = "20260926T120000Z-cold-integrity"
FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "mcp-tool-results"
    / "finalise_cold_characterisation_session.json"
)
STATE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "mcp-tool-results" / "get_roast_state.json"
)
#: The committed state fixture's own session identity, reused by the strict helper.
FIXTURE_SESSION_ID = "4dcc267d8c9041b59fd2b7f83d565609"
COLD_PACKAGE = Path(builders.__file__).parent
NEW_MODULES = tuple(
    COLD_PACKAGE / name
    for name in ("evidence_builders.py", "evidence_store.py", "evidence_reader.py")
)


def make_identity(
    tmp_path: Path, *, pi_root: str, run_id: str = RUN_ID, audio_device: str = "USB microphone"
) -> ColdRunIdentity:
    """Freeze one admitted synthetic cold identity bound to an evidence root."""
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("123e4567-e89b-12d3-a456-426614174000\n", encoding="ascii")
    return freeze_identity(
        run_id=run_id,
        started_at_utc="2026-09-26T12:00:00Z",
        coffee_roaster_mcp_version=REQUIRED_MCP_VERSION,
        python_version="3.11.9",
        platform="linux",
        machine="aarch64",
        operating_system="Linux",
        kernel="6.6.0",
        pi_model="Raspberry Pi 5",
        pi_revision="d04170",
        runtime_config=RuntimeConfigSnapshot.model_validate(
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
        ),
        server_info=ServerInfo(
            product_name="Coffee Roaster MCP",
            package_name="coffee-roaster-mcp",
            version="0.2.1",
            transport="stdio",
            current_phase="bootstrap",
            roaster_driver="hottop_kn8828b_2k_plus",
            first_crack_mode="audio",
            bootstrap_safe=True,
            available_bootstrap_tools=("get_server_info",),
            started_at_utc="2026-09-26T12:00:00Z",
        ),
        device_config=MCPDeviceConfig(recording_devices=(audio_device,)),
        build_provenance=AgentBuildProvenance(
            source_revision="b" * 40,
            source_tree_dirty=False,
            artefact_kind=ColdArtefactKind.WHEEL,
            artefact_sha256="a" * 64,
        ),
        effective_mcp_profile=EffectiveMCPProfile(
            source_sha256="c" * 64,
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
        ),
        audio_device_identity=audio_device,
        serial_port_path="/dev/ttyUSB0",
        controller_tick_seconds=1.0,
        pi_evidence_root=pi_root,
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


def audio_payload() -> dict[str, schema.ColdJsonValue]:
    """Return one complete strict per-tick audio payload plus one unknown key."""
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
        "future_audio_key": {"nested": [1, 2.5, "x"]},
    }


def device_state(**overrides: object) -> ColdTickDeviceState:
    """Return one strict, safe-zero cold device projection with vendor data."""
    values: dict[str, object] = {
        "driver": "mock",
        "connected": True,
        "bean_temp_c": 21.5,
        "env_temp_c": 22.0,
        "heat_level_percent": 0,
        "fan_level_percent": 0,
        "cooling_on": False,
        "raw_vendor_data": {"packet": "abc", "count": 3},
    }
    values.update(overrides)
    return ColdTickDeviceState.model_validate(values)


def roast_fan_state(
    outcome: ColdRoastFanOutcome = ColdRoastFanOutcome.OBSERVED, level: int | None = 0
) -> ColdTickRoastFanObservation:
    """Return one strict commanded roast-fan observation."""
    return ColdTickRoastFanObservation(outcome=outcome, roast_fan_level_percent=level)


def tolerant_state() -> RoastSessionState:
    """Return the committed tolerant state as a cold session.

    Its session fields equal ``session_metadata()``; its device values
    deliberately differ from ``device_state``.
    """
    document = json.loads(STATE_FIXTURE_PATH.read_text(encoding="utf-8"))
    document["session_purpose"] = "cold_characterisation"
    return RoastSessionState.model_validate(document)


def session_metadata(**overrides: object) -> ColdTickSessionMetadata:
    """Return one strict cold session projection coherent with ``tolerant_state``."""
    values: dict[str, object] = {
        "session_id": FIXTURE_SESSION_ID,
        "active": True,
        "session_purpose": "cold_characterisation",
        "phase": "roasting",
        "elapsed_monotonic_seconds": 0.059,
    }
    values.update(overrides)
    return ColdTickSessionMetadata.model_validate(values)


def observation(
    device: ColdTickDeviceState | None,
    *,
    roast_fan: ColdTickRoastFanObservation | None = None,
    audio: dict[str, schema.ColdJsonValue] | None = None,
    session: ColdTickSessionMetadata | None = None,
) -> ColdTickObservation:
    """Return one strict cold tick observation; ``device=None`` is absent telemetry."""
    return ColdTickObservation(
        state=tolerant_state(),
        audio=schema.project_tick_audio(audio_payload() if audio is None else audio),
        device=device,
        roast_fan=roast_fan_state() if roast_fan is None else roast_fan,
        session=session_metadata() if session is None else session,
    )


def finalisation_payload(*, streaming: bool | None = False) -> dict[str, typing.Any]:
    """Return the committed 0.2.1 result, optionally streaming or untrusted."""
    payload = typing.cast(dict[str, typing.Any], json.loads(FIXTURE_PATH.read_text()))
    final = payload["final_driver_evidence"]
    if streaming is None:
        final["outcome"] = "unreadable"
        final["error"] = "driver_state_unreadable"
        final["evidence"] = None
    elif streaming:
        final["evidence"]["command_streaming_required"] = True
    return payload


def finalisation_result(*, streaming: bool | None = False) -> SessionFinalisationResult:
    """Return one strictly parsed finalisation result."""
    return SessionFinalisationResult.model_validate_json(
        json.dumps(finalisation_payload(streaming=streaming))
    )


def host_sample() -> HostBoundSample:
    """Return one complete host-bound sample."""
    return HostBoundSample(
        captured_at_utc="2026-09-26T12:00:01Z",
        monotonic_seconds=2.0,
        soc_temp_c=45.5,
        throttled_word_hex="0x0",
        mem_available_bytes=1_000_000,
        free_bytes=2_000_000,
    )


def header_for(
    tmp_path: Path, root: str, phase: schema.ColdPhaseKind, *, run_id: str = RUN_ID
) -> schema.ColdRunHeader:
    """Build one phase header for an evidence root."""
    return builders.build_run_header(
        identity=make_identity(tmp_path, pi_root=root, run_id=run_id),
        phase=phase,
        recorded_at_utc="2026-09-26T12:00:00Z",
        monotonic_seconds=1.0,
    )


def tick_for(header: schema.ColdRunHeader, tick: int = 0) -> schema.ColdTickRecord:
    """Build one tick record for a header."""
    return builders.build_tick_record(
        header=header,
        tick=tick,
        recorded_at_utc="2026-09-26T12:00:01Z",
        monotonic_seconds=2.0 + tick,
        observation=observation(device_state()),
    )


def host_for(header: schema.ColdRunHeader) -> schema.ColdHostRecord:
    """Build one host record for a header."""
    return builders.build_host_record(
        header=header,
        sample=host_sample(),
        recorded_at_utc="2026-09-26T12:00:01Z",
        monotonic_seconds=2.0,
    )


def finalisation_for(
    header: schema.ColdRunHeader, *, streaming: bool | None = False
) -> schema.ColdFinalisationRecord:
    """Build one finalisation record for a header."""
    return builders.build_finalisation_record(
        header=header,
        result=finalisation_result(streaming=streaming),
        recorded_at_utc="2026-09-26T12:10:00Z",
        monotonic_seconds=600.0,
    )


def advisory_for(header: schema.ColdRunHeader) -> schema.ColdAdvisoryRecord:
    """Build one observation-only advisory record directly (slice 5 owns its builder)."""
    return schema.ColdAdvisoryRecord(
        schema_version=1,
        stream="advisory",
        run_id=header.run_id,
        phase=header.phase,
        recorded_at_utc="2026-09-26T12:00:02Z",
        monotonic_seconds=3.0,
        identity_sha256=header.identity_sha256,
        requested_heat=0,
        requested_fan=0,
        should_drop=False,
        confidence=0.5,
        latency_seconds=0.25,
        evaluation=schema.ColdSafetyEvaluation(
            rule="observation_only",
            verdict=schema.ColdSafetyVerdict.REJECT,
            input_heat=0,
            input_fan=0,
            adjusted_heat=None,
            adjusted_fan=None,
            reason="observation only",
        ),
        failure=None,
    )


def abort_for(
    header: schema.ColdRunHeader,
    domain: schema.ColdAbortDomain = schema.ColdAbortDomain.OPERATOR,
    reason: typing.Any = schema.ColdOperatorAbortReason.OPERATOR_STOP,
) -> schema.ColdAbortRecord:
    """Build one typed abort record directly (slice 4 owns its builder)."""
    return schema.ColdAbortRecord(
        schema_version=1,
        stream="abort",
        run_id=header.run_id,
        phase=header.phase,
        recorded_at_utc="2026-09-26T12:05:00Z",
        monotonic_seconds=300.0,
        identity_sha256=header.identity_sha256,
        domain=domain,
        reason=reason,
    )


def test_header_envelope_is_the_exact_identity_digest_input(tmp_path: Path) -> None:
    """The header retains the identity's canonical bytes and its delivered digest."""
    root = str(tmp_path.resolve())
    identity = make_identity(tmp_path, pi_root=root)
    header = builders.build_run_header(
        identity=identity,
        phase=schema.ColdPhaseKind.RECORDING_OFF,
        recorded_at_utc="2026-09-26T12:00:00Z",
        monotonic_seconds=1.0,
    )
    expected = schema._canonical_json(identity.model_dump(mode="json"))  # pyright: ignore[reportPrivateUsage]
    assert header.identity.canonical_json == expected
    assert header.identity_sha256 == identity_sha256(identity) == header.identity.sha256
    assert hashlib.sha256(expected.encode()).hexdigest() == header.identity_sha256
    assert header.run_id == identity.run_id
    assert header.phase is schema.ColdPhaseKind.RECORDING_OFF


def test_canonical_helper_is_byte_equal_to_the_schema_helper() -> None:
    """The store's canonical form is exactly the schema's private canonical form."""
    samples: list[object] = [
        {"b": [1, 2.5, None, True], "a": "é ", "c": {"z": -0.0, "y": 1e-7}},
        [],
        "x",
    ]
    for sample in samples:
        assert store.canonical_json(sample) == schema._canonical_json(sample)  # pyright: ignore[reportPrivateUsage]


def test_non_conforming_identity_run_id_refuses(tmp_path: Path) -> None:
    """An identity whose run id is outside the record grammar never becomes a header."""
    identity = make_identity(tmp_path, pi_root=str(tmp_path), run_id="cold-1")
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        builders.build_run_header(
            identity=identity,
            phase=schema.ColdPhaseKind.RECORDING_OFF,
            recorded_at_utc="2026-09-26T12:00:00Z",
            monotonic_seconds=1.0,
        )
    assert raised.value.failure is store.ColdEvidenceStoreFailure.RUN_ID_MISMATCHED


def test_header_digest_disagreement_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest that does not hash the canonical bytes never becomes a header."""
    identity = make_identity(tmp_path, pi_root=str(tmp_path))

    def wrong_digest(_identity: ColdRunIdentity) -> str:
        return "0" * 64

    monkeypatch.setattr(builders, "identity_sha256", wrong_digest)
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        builders.build_run_header(
            identity=identity,
            phase=schema.ColdPhaseKind.RECORDING_OFF,
            recorded_at_utc="2026-09-26T12:00:00Z",
            monotonic_seconds=1.0,
        )
    assert raised.value.failure is store.ColdEvidenceStoreFailure.IDENTITY_DIGEST_MISMATCHED


def build_tick(
    header: schema.ColdRunHeader, source: ColdTickObservation, tick: int = 7
) -> schema.ColdTickRecord:
    """Build one tick from an observation through the real builder."""
    return builders.build_tick_record(
        header=header,
        tick=tick,
        recorded_at_utc="2026-09-26T12:00:08Z",
        monotonic_seconds=9.0,
        observation=source,
    )


def test_tick_inherits_header_and_retains_raw_extras(tmp_path: Path) -> None:
    """Ticks copy the strict device and retain unknown audio keys and vendor data."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    state = device_state(heat_level_percent=0, fan_level_percent=0, bean_temp_c=None)
    tick = build_tick(header, observation(state))
    assert (tick.run_id, tick.phase, tick.identity_sha256) == (
        header.run_id,
        header.phase,
        header.identity_sha256,
    )
    assert tick.raw_audio_extra == {"future_audio_key": {"nested": [1, 2.5, "x"]}}
    assert tick.device is not None
    assert tick.device.raw_vendor_data == {"packet": "abc", "count": 3}
    device = tick.device
    assert (device.bean_temp_c, device.env_temp_c, device.connected, device.cooling_on) == (
        None,
        22.0,
        True,
        False,
    )
    assert (device.heat_level_percent, device.fan_level_percent, tick.tick) == (0, 0, 7)


def test_t_e1_tick_copies_strict_device_and_roast_fan_exactly(tmp_path: Path) -> None:
    """T-E1: all eight device and both roast-fan fields equal their strict sources."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    source = observation(
        device_state(driver="hottop", heat_level_percent=3, fan_level_percent=4),
        roast_fan=roast_fan_state(ColdRoastFanOutcome.OBSERVED, 5),
    )
    assert source.device is not None
    tolerant = source.state.device_state
    assert tolerant is not None
    assert (tolerant.heat_level_percent, tolerant.fan_level_percent) != (3, 4)
    tick = build_tick(header, source)
    assert tick.device is not None
    for name in ColdTickDeviceState.model_fields:
        copied = getattr(tick.device, name)
        original = getattr(source.device, name)
        assert copied == original, name
        assert type(copied) is type(original), name
    assert tick.device.raw_vendor_data is not source.device.raw_vendor_data
    assert type(tick.roast_fan) is schema.ColdTickRoastFanEvidence
    assert tick.roast_fan.outcome is schema.ColdTickRoastFanOutcome.OBSERVED
    assert tick.roast_fan.roast_fan_level_percent == 5
    assert type(tick.roast_fan.roast_fan_level_percent) is int
    assert tick.audio == source.audio.audio


@pytest.mark.parametrize("outcome", list(ColdRoastFanOutcome))
def test_t_e1_every_roast_fan_outcome_maps_to_its_schema_member(
    tmp_path: Path, outcome: ColdRoastFanOutcome
) -> None:
    """T-E1: each of D197's five outcomes maps by exact value; only observed has a level."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    level = 0 if outcome is ColdRoastFanOutcome.OBSERVED else None
    tick = build_tick(
        header, observation(device_state(), roast_fan=roast_fan_state(outcome, level))
    )
    assert tick.roast_fan.outcome is schema.ColdTickRoastFanOutcome(outcome.value)
    assert tick.roast_fan.roast_fan_level_percent == level


def test_t_e2_absent_device_is_recorded_as_none_never_zeroed(tmp_path: Path) -> None:
    """T-E2: ``device=None`` stays ``None``; no zero, previous, or tolerant substitute."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    source = observation(None, roast_fan=roast_fan_state(ColdRoastFanOutcome.UNREADABLE, None))
    assert source.state.device_state is not None
    tick = build_tick(header, source)
    assert tick.device is None
    assert tick.model_dump(mode="json")["device"] is None
    assert tick.roast_fan.outcome is schema.ColdTickRoastFanOutcome.UNREADABLE
    assert tick.roast_fan.roast_fan_level_percent is None


def test_t_e3_unsafe_device_values_are_retained_without_a_verdict(tmp_path: Path) -> None:
    """T-E3: unsafe or implausible values are recorded faithfully, never refused."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    source = observation(
        device_state(
            heat_level_percent=150, fan_level_percent=-1, connected=False, cooling_on=True
        ),
        roast_fan=roast_fan_state(ColdRoastFanOutcome.OBSERVED, 100),
    )
    tick = build_tick(header, source)
    assert tick.device is not None
    assert (
        tick.device.heat_level_percent,
        tick.device.fan_level_percent,
        tick.device.connected,
        tick.device.cooling_on,
    ) == (150, -1, False, True)
    assert tick.roast_fan.roast_fan_level_percent == 100
    assert not {"verdict", "clean", "safe_zero"} & set(schema.ColdTickRecord.model_fields)


def test_tick_builder_has_no_path_that_omits_the_projection() -> None:
    """``observation`` is a required keyword; raw audio extras cannot default."""
    parameters = inspect.signature(builders.build_tick_record).parameters
    assert tuple(parameters) == (
        "header",
        "tick",
        "recorded_at_utc",
        "monotonic_seconds",
        "observation",
    )
    parameter = parameters["observation"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(builders.build_tick_record)
    assert "raw_audio_extra=dict(projection.raw_audio_extra)" in source


_TOLERANT_ATTRIBUTES = frozenset({"state", "device_state", "first_crack_status"})


def test_t_e9_tick_builder_never_reads_the_tolerant_state() -> None:
    """T-E9: no tolerant attribute read and no tolerant device-state name in the builders."""
    tree = ast.parse((COLD_PACKAGE / "evidence_builders.py").read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert attributes & _TOLERANT_ATTRIBUTES == set()
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    aliases = {node.name for node in ast.walk(tree) if isinstance(node, ast.alias)}
    assert "RoasterDeviceState" not in names | aliases


def test_t_e9_tick_builder_refuses_wrong_observation_types(tmp_path: Path) -> None:
    """T-E9: only an exact ``ColdTickObservation`` with exact strict parts is accepted."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    genuine = observation(device_state())

    class ObservationSubclass(ColdTickObservation):
        """A non-exact observation type."""

    class DeviceSubclass(ColdTickDeviceState):
        """A non-exact device type."""

    class RoastFanSubclass(ColdTickRoastFanObservation):
        """A non-exact roast-fan type."""

    subclass = ObservationSubclass(
        state=genuine.state,
        audio=genuine.audio,
        device=genuine.device,
        roast_fan=genuine.roast_fan,
        session=genuine.session,
    )
    assert genuine.device is not None
    wrong_device = typing.cast(typing.Any, ColdTickObservation).model_construct(
        state=genuine.state,
        audio=genuine.audio,
        device=DeviceSubclass(**dict(genuine.device)),
        roast_fan=genuine.roast_fan,
        session=genuine.session,
    )
    tolerant_device = typing.cast(typing.Any, ColdTickObservation).model_construct(
        state=genuine.state,
        audio=genuine.audio,
        device=genuine.state.device_state,
        roast_fan=genuine.roast_fan,
        session=genuine.session,
    )
    wrong_fan = typing.cast(typing.Any, ColdTickObservation).model_construct(
        state=genuine.state,
        audio=genuine.audio,
        device=genuine.device,
        roast_fan=RoastFanSubclass(**dict(genuine.roast_fan)),
        session=genuine.session,
    )
    wrong_audio = typing.cast(typing.Any, ColdTickObservation).model_construct(
        state=genuine.state,
        audio=genuine.audio.model_dump(),
        device=genuine.device,
        roast_fan=genuine.roast_fan,
        session=genuine.session,
    )
    for bad in (
        genuine.state,
        subclass,
        genuine.model_dump(),
        wrong_device,
        tolerant_device,
        wrong_fan,
        wrong_audio,
    ):
        with pytest.raises(schema.ColdEvidenceError) as raised:
            build_tick(header, typing.cast(typing.Any, bad))
        assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED


def test_oversized_raw_maps_refuse_without_truncation(tmp_path: Path) -> None:
    """Oversized vendor data or raw audio extras refuse; nothing is truncated."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    big_vendor = device_state(raw_vendor_data={"blob": "x" * (schema.MAX_VENDOR_BLOB_BYTES + 1)})
    with pytest.raises(schema.ColdEvidenceError) as raised:
        build_tick(header, observation(big_vendor))
    assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_VENDOR_BLOB_TOO_LARGE
    genuine = observation(device_state())
    oversized = schema.ColdTickProjection(
        audio=genuine.audio.audio,
        raw_audio_extra={"blob": "y" * (schema.MAX_RAW_AUDIO_EXTRA_BYTES + 1)},
    )
    with pytest.raises(schema.ColdEvidenceError) as raised:
        build_tick(header, genuine.model_copy(update={"audio": oversized}))
    assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE


def test_host_record_copies_the_six_sample_fields(tmp_path: Path) -> None:
    """Host records copy exactly the six host-bound fields under the header binding."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_OFF)
    record = host_for(header)
    assert record.sample.model_dump() == host_sample().model_dump()
    assert record.identity_sha256 == header.identity_sha256


@pytest.mark.parametrize(
    ("streaming", "observed", "branch"),
    [
        (False, False, schema.ColdCapabilityBranch.NON_STREAMING),
        (True, True, schema.ColdCapabilityBranch.STREAMING),
        (None, None, None),
    ],
)
def test_finalisation_index_is_derived_from_the_envelope(
    tmp_path: Path,
    streaming: bool | None,
    observed: bool | None,
    branch: schema.ColdCapabilityBranch | None,
) -> None:
    """The five scalars come only from the envelope; absent evidence stays ``None``."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    record = finalisation_for(header, streaming=streaming)
    payload = finalisation_payload(streaming=streaming)
    assert record.session_id == payload["session_id"]
    assert record.status is schema.ColdFinalisationStatus(payload["status"])
    assert record.clean is payload["clean"]
    assert record.observed_command_streaming_required is observed
    assert record.applied_branch is branch
    reparsed = SessionFinalisationResult.model_validate_json(record.envelope.canonical_json)
    assert reparsed == finalisation_result(streaming=streaming)


def test_fixture_round_trips_byte_identically_through_canonical_form() -> None:
    """The escalation precondition: dump, canonicalise, and re-parse are lossless."""
    result = finalisation_result()
    canonical = store.canonical_json(result.model_dump(mode="json"))
    reparsed = SessionFinalisationResult.model_validate_json(canonical)
    assert reparsed == result
    assert store.canonical_json(reparsed.model_dump(mode="json")) == canonical


def test_finalisation_builder_refuses_a_non_round_tripping_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the envelope would not re-parse to the result, no record is built."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    other = finalisation_result(streaming=True)

    def other_result(_envelope: schema.ColdSealedEnvelope) -> SessionFinalisationResult:
        return other

    monkeypatch.setattr(builders, "parse_finalisation_envelope", other_result)
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        finalisation_for(header)
    assert raised.value.failure is store.ColdEvidenceStoreFailure.FINALISATION_INDEX_MISMATCHED


def test_builders_refuse_wrong_input_types(tmp_path: Path) -> None:
    """Builders accept only the exact typed inputs they bind."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    calls: list[typing.Callable[[], object]] = [
        lambda: builders.build_host_record(
            header=typing.cast(typing.Any, header.model_dump()),
            sample=host_sample(),
            recorded_at_utc="t",
            monotonic_seconds=1.0,
        ),
        lambda: builders.build_tick_record(
            header=header,
            tick=0,
            recorded_at_utc="t",
            monotonic_seconds=1.0,
            observation=typing.cast(typing.Any, observation(device_state()).model_dump()),
        ),
        lambda: builders.build_finalisation_record(
            header=header,
            result=typing.cast(typing.Any, finalisation_payload()),
            recorded_at_utc="t",
            monotonic_seconds=1.0,
        ),
        lambda: builders.build_run_header(
            identity=typing.cast(typing.Any, {}),
            phase=schema.ColdPhaseKind.RECORDING_ON,
            recorded_at_utc="t",
            monotonic_seconds=1.0,
        ),
    ]
    for call in calls:
        with pytest.raises(schema.ColdEvidenceError) as raised:
            call()
        assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED


def test_builder_validation_failures_are_closed_without_chains(tmp_path: Path) -> None:
    """A pydantic construction failure becomes a chain-free closed evidence error."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    with pytest.raises(schema.ColdEvidenceError) as raised:
        builders.build_host_record(
            header=header,
            sample=host_sample(),
            recorded_at_utc="x" * (schema.MAX_TEXT_FIELD_BYTES + 1),
            monotonic_seconds=1.0,
        )
    assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    with pytest.raises(schema.ColdEvidenceError) as raised:
        builders._envelope(  # pyright: ignore[reportPrivateUsage]
            schema.ColdEnvelopeKind.IDENTITY, "{}", "not-a-digest"
        )
    assert raised.value.__context__ is None


def test_class_h_capability_attribute_is_read_only_in_the_sole_predicate() -> None:
    """Class H: the capability attribute is read exactly once in the cold package."""
    reads: list[str] = []
    for path in sorted(COLD_PACKAGE.glob("*.py")):
        for line in path.read_text().splitlines():
            if re.search(r"\.command_streaming_required\b", line):
                reads.append(f"{path.name}:{line.strip()}")
    assert reads == ["mcp.py:return evidence.command_streaming_required"]


def _identifiers(source: str) -> set[str]:
    """Return every identifier a module defines, imports, or references."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


# A1-4d: the tick builder copies D197's recorded roast-fan read ``outcome`` and
# its schema-owned enum.  Exactly these two identifiers are exempt, and only in
# ``evidence_builders.py``; every other identifier stays under the token guard.
_ROAST_FAN_OUTCOME_IDENTIFIERS: typing.Final[frozenset[str]] = frozenset(
    {"ColdTickRoastFanOutcome", "outcome"}
)


def _token_violations(module_name: str, identifiers: set[str]) -> set[str]:
    """Return every identifier breaching the verdict/evaluation/report/outcome token rules."""
    violations: set[str] = set()
    for identifier in identifiers:
        lowered = identifier.lower()
        if (
            any(token in lowered for token in ("verdict", "evaluat", "report"))
            or "outcome" in lowered
            and not (
                module_name == "evidence_builders.py"
                and identifier in _ROAST_FAN_OUTCOME_IDENTIFIERS
            )
        ):
            violations.add(identifier)
    return violations


def _outcome_receiver_violations(source: str) -> list[str]:
    """Return every ``.outcome`` read whose receiver is not ``.roast_fan``."""
    reads = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr == "outcome"
    ]
    if not reads:
        return ["missing roast_fan.outcome read"]
    return [
        ast.unparse(node)
        for node in reads
        if not (isinstance(node.value, ast.Attribute) and node.value.attr == "roast_fan")
    ]


def test_token_exception_is_exact_and_receiver_pinned() -> None:
    """T-E14b: the exception is exact set membership, builder-only, and receiver-pinned."""
    builder = "evidence_builders.py"
    assert _token_violations(builder, {"ColdTickRoastFanOutcome", "outcome"}) == set()
    for name in (
        "phase_outcome",
        "ColdCheckOutcome",
        "outcome_check",
        "ColdTickRoastFanOutcomeX",
        "Outcome",
    ):
        assert _token_violations(builder, {name}) == {name}
    banned = {"evaluate_roast_fan", "roast_fan_verdict", "report_row"}
    assert _token_violations(builder, banned) == banned
    assert _token_violations("evidence_store.py", {"outcome"}) == {"outcome"}
    assert _token_violations("evidence_reader.py", {"ColdTickRoastFanOutcome"}) == {
        "ColdTickRoastFanOutcome"
    }
    assert _outcome_receiver_violations("x = observation.roast_fan.outcome") == []
    assert len(_outcome_receiver_violations("x = header.outcome")) == 1
    assert (
        len(_outcome_receiver_violations("x = observation.roast_fan.outcome\ny = check.outcome"))
        == 1
    )
    assert _outcome_receiver_violations("x = 1") == ["missing roast_fan.outcome read"]


def test_new_modules_contain_no_actuator_verdict_or_limit_names() -> None:
    """Classes E and K: no actuator reach, clean conjunction, verdict, report, or limit."""
    forbidden_text = (
        "set_heat",
        "set_fan",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "mark_first_crack",
        "set_targets",
        "call_tool",
        "RoasterControlAdapter",
        "MAX_CONSECUTIVE_OVERFLOW",
        "LOST_AUDIO_MS",
        "FATAL_STREAK",
        "EFFECTIVE_HOP",
        "HOST_MAX",
        "HOST_MIN",
        "finalisation_is_clean",
    )
    for path in NEW_MODULES:
        source = path.read_text()
        for name in forbidden_text:
            assert name not in source, (path.name, name)
        assert _token_violations(path.name, _identifiers(source)) == set(), path.name
    assert _outcome_receiver_violations((COLD_PACKAGE / "evidence_builders.py").read_text()) == []
    assert frozenset({"ColdTickRoastFanOutcome", "outcome"}) == _ROAST_FAN_OUTCOME_IDENTIFIERS


_COLD = "roastpilot_agent.cold_characterisation."
_STDLIB_ALLOWED = frozenset(
    {"os", "stat", "hashlib", "json", "math", "enum", "typing", "collections.abc", "pydantic"}
)
_MODULE_IMPORT_ALLOW_LIST: dict[str, frozenset[str]] = {
    "evidence_store.py": frozenset(
        {
            _COLD + "evidence_schema",
            _COLD + "mcp",
            _COLD + "evidence_lifecycle",
            _COLD + "evidence_advisory",
        }
    ),
    "evidence_reader.py": frozenset(
        {
            _COLD + "evidence_schema",
            _COLD + "evidence_store",
            _COLD + "evidence_lifecycle",
            _COLD + "evidence_advisory",
        }
    ),
    "evidence_builders.py": frozenset(
        {
            _COLD + "evidence_schema",
            _COLD + "evidence_store",
            _COLD + "identity",
            _COLD + "mcp",
            _COLD + "host",
            _COLD + "evidence_lifecycle",
        }
    ),
}
_RESTRICTED_NAMES: dict[str, frozenset[str]] = {
    _COLD + "host": frozenset({"HostBoundSample"}),
}


def _direct_imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(module, imported names)`` for every import statement in a module."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None, path.name
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def test_new_modules_import_only_their_ratified_allow_lists() -> None:
    """Each new module imports only the ratified stdlib set and its allowed cold modules."""
    for path in NEW_MODULES:
        allowed = _STDLIB_ALLOWED | _MODULE_IMPORT_ALLOW_LIST[path.name]
        for module, names in _direct_imports(path):
            assert module in allowed, (path.name, module)
            restricted = _RESTRICTED_NAMES.get(module)
            if restricted is not None:
                assert set(names) <= restricted, (path.name, module, names)
            assert module.split(".")[0] not in {
                "subprocess",
                "socket",
                "shutil",
                "tempfile",
                "httpx",
            }


def _reachable_package_modules(
    roots: tuple[str, ...], *, stop: frozenset[str] = frozenset()
) -> set[str]:
    """Statically follow first-party imports from roots, not expanding ``stop`` modules."""
    source_root = Path(builders.__file__).parents[2]

    def locate(module: str) -> Path | None:
        candidate = source_root / (module.replace(".", "/") + ".py")
        if candidate.is_file():
            return candidate
        package = source_root / module.replace(".", "/") / "__init__.py"
        return package if package.is_file() else None

    seen: set[str] = set()
    pending = list(roots)
    while pending:
        module = pending.pop()
        path = locate(module)
        if module in seen or path is None:
            continue
        seen.add(module)
        if module in stop:
            continue
        for imported, names in _direct_imports_any_level(path, module):
            if imported.startswith("roastpilot_agent"):
                pending.append(imported)
                pending.extend(f"{imported}.{name}" for name in names)
    return seen


def _direct_imports_any_level(path: Path, module: str) -> list[tuple[str, tuple[str, ...]]]:
    """Resolve absolute and relative imports of one first-party module."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package
            for _ in range(max(node.level - 1, 0)):
                base = base.rpartition(".")[0]
            target = (
                node.module
                if node.level == 0
                else ".".join(part for part in (base, node.module or "") if part)
            )
            if target:
                found.append((target, tuple(alias.name for alias in node.names)))
    return found


def test_new_modules_reach_no_orchestration_or_roast_control_module() -> None:
    """Transitively, the new modules never reach controller, safety, API, store, or CLI code."""
    roots = tuple(_COLD + path.stem for path in NEW_MODULES)
    reachable = _reachable_package_modules(roots)
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.api",
        "roastpilot_agent.store",
        "roastpilot_agent.cli",
        "roastpilot_agent.live",
        "roastpilot_agent.replay",
        "roastpilot_agent.appliance.model_install",
    }
    assert reachable & forbidden == set()
    assert _COLD + "evidence_store" in reachable


def test_advisor_is_reached_only_through_the_allowed_identity_module() -> None:
    """The advisor type dependency arrives only via the delivered identity module."""
    roots = tuple(_COLD + path.stem for path in NEW_MODULES)
    assert "roastpilot_agent.advisor" in _reachable_package_modules(roots)
    without_identity = _reachable_package_modules(roots, stop=frozenset({_COLD + "identity"}))
    assert "roastpilot_agent.advisor" not in without_identity


# ------------------------------------ 4f-b abort builder and retained tick session

_CLOSED_MESSAGE = "Cold evidence admission failed."
_SESSION_NAMES = (
    "session_id",
    "active",
    "session_purpose",
    "phase",
    "elapsed_monotonic_seconds",
)


def _assert_closed(error: schema.ColdEvidenceError, failure: schema.ColdEvidenceFailure) -> None:
    """Assert one closed evidence failure with its fixed message and no chain."""
    assert error.failure is failure
    assert error.args == (_CLOSED_MESSAGE,)
    assert error.__cause__ is None
    assert error.__context__ is None


def _build_abort(header: schema.ColdRunHeader, domain: object, reason: object) -> object:
    """Build one abort through the real builder with the shared recording metadata."""
    return builders.build_abort_record(
        header=header,
        domain=typing.cast(typing.Any, domain),
        reason=typing.cast(typing.Any, reason),
        recorded_at_utc="2026-09-26T12:05:00Z",
        monotonic_seconds=300.0,
    )


_LEGAL_ABORT_PAIRS: list[tuple[schema.ColdAbortDomain, enum.Enum]] = [
    (domain, reason)
    for domain, reason_type in (
        (schema.ColdAbortDomain.HOST, schema.ColdHostAbortReason),
        (schema.ColdAbortDomain.IDENTITY, schema.ColdIdentityAbortReason),
        (schema.ColdAbortDomain.EVIDENCE, schema.ColdEvidenceFailure),
        (schema.ColdAbortDomain.MCP, schema.ColdMcpAbortReason),
        (schema.ColdAbortDomain.ADVISOR, schema.ColdAdvisorFailureKind),
        (schema.ColdAbortDomain.OPERATOR, schema.ColdOperatorAbortReason),
        (schema.ColdAbortDomain.ENGINE, schema.ColdEngineAbortReason),
    )
    for reason in typing.cast(type[enum.Enum], reason_type)
]


def test_t_b1_every_legal_pair_builds_equal_to_direct_construction(tmp_path: Path) -> None:
    """T-B1: builder, direct construction, and ``validate_record`` agree on all seven domains."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    assert {domain for domain, _reason in _LEGAL_ABORT_PAIRS} == set(schema.ColdAbortDomain)
    assert schema.ColdAbortDomain.ENGINE in {domain for domain, _reason in _LEGAL_ABORT_PAIRS}
    for domain, reason in _LEGAL_ABORT_PAIRS:
        direct = abort_for(header, domain, reason)
        built = _build_abort(header, domain, reason)
        assert type(built) is schema.ColdAbortRecord
        assert built == direct == schema.validate_record(direct)
        assert (built.run_id, built.phase, built.identity_sha256) == (
            header.run_id,
            header.phase,
            header.identity_sha256,
        )
        assert built.reason is reason


class _HeaderSub(schema.ColdRunHeader):
    """A non-exact header type."""


class _PlainReason(enum.Enum):
    """A foreign plain enum sharing an ENGINE value."""

    CANCELLED = "cancelled"


class _MixinReason(str, enum.Enum):  # noqa: UP042 - the contract's str-mixin fixture, not StrEnum
    """A foreign ``str``-mixin enum sharing an ENGINE value."""

    CANCELLED = "cancelled"


_ENGINE = schema.ColdAbortDomain.ENGINE
_CANCELLED = schema.ColdEngineAbortReason.CANCELLED
_BUILDER_ABORT_CASES: dict[str, tuple[object, object, schema.ColdEvidenceFailure]] = {
    "string-domain": ("engine", _CANCELLED, schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED),
    "mismatched-pair": (
        _ENGINE,
        schema.ColdMcpAbortReason.SESSION_NOT_ACTIVE,
        schema.ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED,
    ),
    "foreign-plain-enum": (
        _ENGINE,
        _PlainReason.CANCELLED,
        schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED,
    ),
    "foreign-str-mixin-enum": (
        _ENGINE,
        _MixinReason.CANCELLED,
        schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED,
    ),
    "raw-string-reason": (
        _ENGINE,
        "cancelled",
        schema.ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED,
    ),
}


@pytest.mark.parametrize("name", list(_BUILDER_ABORT_CASES))
def test_t_b4_abort_builder_refuses_each_forged_pair(tmp_path: Path, name: str) -> None:
    """T-B4: the builder refuses string domains, foreign enums, raw text, and mismatches."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    domain, reason, failure = _BUILDER_ABORT_CASES[name]
    with pytest.raises(schema.ColdEvidenceError) as raised:
        _build_abort(header, domain, reason)
    _assert_closed(raised.value, failure)


def test_t_b4_abort_builder_refuses_a_header_subclass(tmp_path: Path) -> None:
    """T-B4: only an exact ``ColdRunHeader`` binds an abort record."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    subclass = typing.cast(typing.Any, _HeaderSub).model_construct(**dict(header))
    with pytest.raises(schema.ColdEvidenceError) as raised:
        _build_abort(subclass, _ENGINE, _CANCELLED)
    _assert_closed(raised.value, schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def test_t_b5_abort_builder_takes_no_free_form_or_exception_input() -> None:
    """T-B5: keyword-only closed inputs, no handler, and strict validation."""
    tree = ast.parse((COLD_PACKAGE / "evidence_builders.py").read_text(encoding="utf-8"))
    (function,) = (
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_abort_record"
    )
    arguments = function.args
    assert arguments.posonlyargs == [] and arguments.args == []
    assert arguments.vararg is None and arguments.kwarg is None
    assert tuple(argument.arg for argument in arguments.kwonlyargs) == (
        "header",
        "domain",
        "reason",
        "recorded_at_utc",
        "monotonic_seconds",
    )
    for argument in arguments.kwonlyargs:
        assert argument.annotation is not None
        annotation = ast.unparse(argument.annotation)
        assert "Exception" not in annotation, argument.arg
        if argument.arg != "recorded_at_utc":
            assert annotation != "str", argument.arg
    assert not any(isinstance(node, (ast.Try, ast.ExceptHandler)) for node in ast.walk(function))
    strict_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_validate"
        and any(
            keyword.arg == "strict"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    assert len(strict_calls) == 1


def test_t_b15_helpers_are_one_coherent_cold_session() -> None:
    """T-B15: the strict helper's five session values equal the tolerant fixture's."""
    tolerant = tolerant_state()
    strict = session_metadata()
    assert tolerant.session_purpose == "cold_characterisation"
    assert strict.session_id == FIXTURE_SESSION_ID
    for name in _SESSION_NAMES:
        assert getattr(strict, name) == getattr(tolerant, name), name
    assert (strict.active, strict.phase, strict.elapsed_monotonic_seconds) == (
        True,
        "roasting",
        0.059,
    )


def test_t_b6_tick_copies_the_strict_session_exactly(tmp_path: Path) -> None:
    """T-B6: the tick retains the observation's five session values with exact types."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    source = observation(device_state())
    tick = build_tick(header, source)
    for name in ("session_id", "active", "elapsed_monotonic_seconds"):
        copied = getattr(tick.session, name)
        original = getattr(source.session, name)
        assert copied == original, name
        assert type(copied) is type(original), name
    assert tick.session.session_purpose == "cold_characterisation"
    assert type(tick.session.session_purpose) is str
    assert type(tick.session.phase) is schema.ColdTickSessionPhase
    assert tick.session.phase.value == source.session.phase
    assert tuple(schema.ColdTickSessionEvidence.model_fields) == _SESSION_NAMES
    assert (
        schema.ColdTickSessionEvidence.model_fields["session_purpose"].annotation
        == (typing.Literal["cold_characterisation"])
    )


@pytest.mark.parametrize("phase", list(typing.get_args(MCPPhase)))
def test_t_b6_every_mcp_phase_maps_to_its_schema_member(tmp_path: Path, phase: str) -> None:
    """T-B6: each MCP phase value maps by exact value to the schema-owned mirror."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    tick = build_tick(header, observation(device_state(), session=session_metadata(phase=phase)))
    assert tick.session.phase is schema.ColdTickSessionPhase(phase)


_TICK_ADAPTER = pydantic.TypeAdapter(schema.ColdTickRecord)


@pytest.mark.parametrize(
    "update",
    [
        {"active": False},
        {"phase": "fault"},
        {"elapsed_monotonic_seconds": -1.0},
        {"elapsed_monotonic_seconds": 0.0},
    ],
    ids=["inactive", "fault", "negative-clock", "zero-clock"],
)
def test_t_b7_session_values_are_data_not_policy(tmp_path: Path, update: dict[str, object]) -> None:
    """T-B7: inactive, fault, negative, and zero session values are retained exactly."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    source = observation(device_state(), session=session_metadata(**update))
    tick = build_tick(header, source)
    for name, value in update.items():
        retained = getattr(tick.session, name)
        retained_value = retained.value if name == "phase" else retained
        assert retained_value == value
        assert type(retained_value) is type(value)
    line = store.canonical_json(tick.model_dump(mode="json"))
    decoded = _TICK_ADAPTER.validate_json(line, strict=True)
    assert decoded == tick
    assert store.canonical_json(decoded.model_dump(mode="json")) == line


class _StrSub(str):
    """A ``str`` subclass that strict pydantic would otherwise normalise."""


class _SessionMetadataSubclass(ColdTickSessionMetadata):
    """A non-exact strict session metadata type."""


def _forged_session(**update: object) -> ColdTickSessionMetadata:
    """Return unvalidated session metadata with one field replaced."""
    return typing.cast(
        ColdTickSessionMetadata,
        typing.cast(typing.Any, ColdTickSessionMetadata).model_construct(
            **(dict(session_metadata()) | update)
        ),
    )


_FORGED_SESSIONS: dict[str, typing.Callable[[], ColdTickSessionMetadata]] = {
    "a-subclass": lambda: _SessionMetadataSubclass(**dict(session_metadata())),
    "b-roast-purpose": lambda: _forged_session(session_purpose="roast"),
    "c-str-subclass-purpose": lambda: _forged_session(
        session_purpose=_StrSub("cold_characterisation")
    ),
    "d-str-subclass-phase": lambda: _forged_session(phase=_StrSub("roasting")),
    "e-unknown-phase": lambda: _forged_session(phase="bogus"),
    "f-null-phase": lambda: _forged_session(phase=None),
    "g-int-active": lambda: _forged_session(active=1),
    "h-string-active": lambda: _forged_session(active="true"),
    "i-int-clock": lambda: _forged_session(elapsed_monotonic_seconds=1),
    "j-bool-clock": lambda: _forged_session(elapsed_monotonic_seconds=True),
    "k-string-clock": lambda: _forged_session(elapsed_monotonic_seconds="0.5"),
    "l-nan-clock": lambda: _forged_session(elapsed_monotonic_seconds=math.nan),
    "m-inf-clock": lambda: _forged_session(elapsed_monotonic_seconds=math.inf),
    "n-int-session-id": lambda: _forged_session(session_id=1),
    "o-str-subclass-session-id": lambda: _forged_session(session_id=_StrSub(FIXTURE_SESSION_ID)),
}


@pytest.mark.parametrize("name", list(_FORGED_SESSIONS))
def test_t_b8_tick_builder_admits_only_exact_session_metadata(tmp_path: Path, name: str) -> None:
    """T-B8: forged, subclassed, or coercible session metadata never becomes a tick."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    genuine = observation(device_state())
    forged = genuine.model_copy(update={"session": _FORGED_SESSIONS[name]()})
    with pytest.raises(schema.ColdEvidenceError) as raised:
        build_tick(header, forged)
    _assert_closed(raised.value, schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def test_t_b10_session_id_at_the_byte_bound_is_retained_exactly(tmp_path: Path) -> None:
    """T-B10: an ASCII session id of exactly the bound is admitted and never shortened."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    at_bound = "s" * schema.MAX_TEXT_FIELD_BYTES
    tick = build_tick(
        header, observation(device_state(), session=session_metadata(session_id=at_bound))
    )
    assert tick.session.session_id == at_bound
    line = store.canonical_json(tick.model_dump(mode="json"))
    assert _TICK_ADAPTER.validate_json(line, strict=True) == tick


@pytest.mark.parametrize(
    ("session_id", "failure"),
    [
        ("s" * (schema.MAX_TEXT_FIELD_BYTES + 1), schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED),
        ("€" * 683, schema.ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE),
        ("\ud800", schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED),
    ],
    ids=["ascii-over-bound", "multibyte-over-bound", "lone-surrogate"],
)
def test_t_b10_session_id_beyond_the_bound_refuses_without_truncation(
    tmp_path: Path, session_id: str, failure: schema.ColdEvidenceFailure
) -> None:
    """T-B10: an over-bound or unencodable session id refuses with its closed code."""
    header = header_for(tmp_path, str(tmp_path), schema.ColdPhaseKind.RECORDING_ON)
    genuine = observation(device_state())
    forged = genuine.model_copy(update={"session": _forged_session(session_id=session_id)})
    with pytest.raises(schema.ColdEvidenceError) as raised:
        build_tick(header, forged)
    _assert_closed(raised.value, failure)
