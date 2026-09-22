"""Behavioural coverage for fail-closed cold identity freezing."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from roastpilot_agent.advisor import AdvisorDescriptor
from roastpilot_agent.appliance.model_manifest import MANIFEST_FILES, REPO_ID, REVISION
from roastpilot_agent.cold_characterisation.identity import (
    REQUIRED_MCP_VERSION,
    ColdIdentityError,
    ColdIdentityFailure,
    freeze_identity,
    identity_sha256,
)
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo


def _runtime(**changes: object) -> RuntimeConfigSnapshot:
    """Build the already-fetched runtime identity mirror used by freezing."""
    values: dict[str, object] = {
        "config_source": None,
        "roaster_driver": "hottop_kn8828b_2k_plus",
        "roaster_port": "/dev/ttyUSB0",
        "roaster_baudrate": 115200,
        "temperature_unit": "celsius",
        "command_interval_seconds": 0.3,
        "first_crack_mode": "audio",
        "model_repo_id": REPO_ID,
        "model_precision": "int8",
        "allow_manual_override": False,
        "log_dir": "logs",
        "sample_interval_seconds": 5.0,
        "auto_t0_detection_enabled": False,
        "auto_t0_drop_threshold_c": 25.0,
    }
    values.update(changes)
    return RuntimeConfigSnapshot.model_validate(values)


def _server() -> ServerInfo:
    """Build the already-fetched server identity mirror used by freezing."""
    return ServerInfo(
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


def _freeze(tmp_path: Path, **changes: object):
    """Freeze a valid identity using only a test-local boot identifier source."""
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("123e4567-e89b-12d3-a456-426614174000\n", encoding="ascii")
    arguments: dict[str, object] = {
        "run_id": "cold-1",
        "started_at_utc": "2026-09-22T00:00:00Z",
        "coffee_roaster_mcp_version": REQUIRED_MCP_VERSION,
        "python_version": "3.11.9",
        "platform": "linux",
        "machine": "aarch64",
        "operating_system": "Linux",
        "kernel": "6.6.0",
        "pi_model": "Raspberry Pi 5",
        "pi_revision": "d04170",
        "runtime_config": _runtime(),
        "server_info": _server(),
        "device_config": MCPDeviceConfig(recording_devices=("USB microphone",)),
        "audio_device_identity": "USB microphone",
        "serial_port_path": "/dev/ttyUSB0",
        "controller_tick_seconds": 1.0,
        "pi_evidence_root": "/var/lib/roastpilot-agent/evidence",
        "laptop_evidence_root": "/Volumes/evidence",
        "advisor_descriptor": AdvisorDescriptor(
            provider="openrouter", model="test/model", prompt_version="v1"
        ),
        "credential_env_var_name": "OPENROUTER_API_KEY",
        "credential_present": True,
        "stimulus_block": "Tap the empty drum once.",
        "operator_host_notes": "Active cooler installed.",
        "operator_psu_notes": "Official PSU connected.",
        "operator_cooling_notes": "Cooler audible.",
        "boot_id_path": boot_id_path,
    }
    arguments.update(changes)
    return freeze_identity(**arguments)  # type: ignore[arg-type]


def _assert_failure(tmp_path: Path, failure: ColdIdentityFailure, **changes: object) -> None:
    """Assert that a freezing input rejects with its closed failure reason."""
    with pytest.raises(ColdIdentityError) as raised:
        _freeze(tmp_path, **changes)
    assert raised.value.failure is failure


def test_freeze_records_manifest_and_credential_name_without_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity contains the permitted name and boolean, never an environment value."""
    secret = "sk-this-must-not-appear-in-the-frozen-identity"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    identity = _freeze(tmp_path)
    dumped = identity.model_dump(mode="json")
    assert dumped["credential_env_var_name"] == "OPENROUTER_API_KEY"
    assert dumped["credential_present"] is True
    assert secret not in repr(dumped)
    assert identity.model_repo_id == REPO_ID
    assert identity.model_revision == REVISION
    assert [(entry.relative_path, entry.sha256) for entry in identity.model_manifest] == [
        (entry.relative_path, entry.sha256) for entry in MANIFEST_FILES
    ]
    assert "os.environ[" not in inspect.getsource(freeze_identity)


@pytest.mark.parametrize("version", ["0.2.0", "0.2.2", "0.3.0", "0.2.1.post1", "0.2.10", " 0.2.1"])
def test_freeze_requires_exact_mcp_version(tmp_path: Path, version: str) -> None:
    """Only the exact ratified MCP release may be frozen."""
    _assert_failure(
        tmp_path, ColdIdentityFailure.MCP_VERSION_NOT_PINNED, coffee_roaster_mcp_version=version
    )


@pytest.mark.parametrize("unit", ["F", "fahrenheit", "celsius-ish", "", "Celsius"])
def test_freeze_requires_exact_celsius_token(tmp_path: Path, unit: str) -> None:
    """Forward-tolerant runtime strings are admitted only by whole-token membership."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.TEMPERATURE_UNIT_NOT_CELSIUS,
        runtime_config=_runtime(temperature_unit=unit),
    )


@pytest.mark.parametrize("devices", [None, (), ("first", "second")])
def test_freeze_requires_one_recording_device(
    tmp_path: Path, devices: tuple[str, ...] | None
) -> None:
    """A characterisation run refuses zero or multiple recording devices."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.RECORDING_DEVICE_NOT_SINGLE,
        device_config=MCPDeviceConfig(recording_devices=devices),
    )


@pytest.mark.parametrize("mode", ["disabled", "manual"])
def test_freeze_refuses_non_audio_inference_mode(tmp_path: Path, mode: str) -> None:
    """Disabled detector configuration cannot start a qualifying cold run."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=_runtime(first_crack_mode=mode),
    )


def test_freeze_refuses_non_int8_precision_and_committed_bare_fixture_shape(tmp_path: Path) -> None:
    """The captured bare runtime remains an inadmissible disabled-detector identity."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=_runtime(model_precision="fp32"),
    )
    fixture_runtime = _runtime(first_crack_mode="disabled", model_precision="int8")
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=fixture_runtime,
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "123E4567-E89B-12D3-A456-426614174000\n",
        "123e4567-e89b-12d3-a456-426614174000\nsecond",
        "123e4567-e89b-12d3-a456-42661417400",
        "é",
        "x" * 129,
    ],
)
def test_freeze_refuses_malformed_or_oversized_boot_id(tmp_path: Path, content: str) -> None:
    """The byte-capped, anchored boot-ID grammar has no default identity."""
    boot_id_path = tmp_path / "bad_boot_id"
    boot_id_path.write_text(content, encoding="utf-8")
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.BOOT_ID_UNREADABLE
        if content == "x" * 129
        else ColdIdentityFailure.BOOT_ID_MALFORMED,
        boot_id_path=boot_id_path,
    )


def test_freeze_refuses_missing_boot_id(tmp_path: Path) -> None:
    """An unreadable boot source cannot become an unknown identity field."""
    _assert_failure(
        tmp_path, ColdIdentityFailure.BOOT_ID_UNREADABLE, boot_id_path=tmp_path / "missing"
    )


def test_freeze_requires_allowed_credential_name(tmp_path: Path) -> None:
    """Only the declared OpenRouter credential name may be represented."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.CREDENTIAL_NAME_NOT_ALLOWED,
        credential_env_var_name="OTHER_TOKEN",
    )


@pytest.mark.parametrize(
    "field",
    ["stimulus_block", "operator_host_notes", "operator_psu_notes", "operator_cooling_notes"],
)
@pytest.mark.parametrize(
    "text",
    ["sk-abcdefghijklmnopqrstuvwx", "api_key=secret-value", "contains\x01control", "x" * 2001],
)
def test_freeze_rejects_secret_shaped_or_invalid_operator_text(
    tmp_path: Path, field: str, text: str
) -> None:
    """All operator free text raises rather than redacting secret-shaped content."""
    _assert_failure(tmp_path, ColdIdentityFailure.OPERATOR_TEXT_REJECTED, **{field: text})


def test_identity_hash_is_canonical_stable_and_sensitive(tmp_path: Path) -> None:
    """Canonical JSON hashing is stable for equals and changes for one field."""
    identity = _freeze(tmp_path)
    equivalent = _freeze(tmp_path)
    changed = _freeze(tmp_path, pi_revision="d04171")
    digest = identity_sha256(identity)
    assert digest == identity_sha256(equivalent)
    assert digest != identity_sha256(changed)
    assert len(digest) == 64
    assert digest == digest.lower()


def test_identity_rejects_non_finite_controller_tick(tmp_path: Path) -> None:
    """The frozen finite model refuses non-finite values before hashing."""
    with pytest.raises(ValidationError):
        _freeze(tmp_path, controller_tick_seconds=float("nan"))
