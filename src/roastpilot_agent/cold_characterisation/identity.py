"""Fail-closed frozen identity for cold-characterisation evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import Enum
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, model_validator

from roastpilot_agent import __version__
from roastpilot_agent.advisor import AdvisorDescriptor
from roastpilot_agent.appliance.model_manifest import MANIFEST_FILES, REPO_ID, REVISION
from roastpilot_agent.config import FINITE_NUMERIC_MODEL_CONFIG, MCPDeviceConfig
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo

REQUIRED_MCP_VERSION: Final = "0.2.1"
BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
_MAX_BOOT_ID_BYTES: Final = 128
_MAX_OPERATOR_TEXT_LENGTH: Final = 2000
_ALLOWED_CELSIUS_TOKENS: Final = frozenset({"celsius"})
_ALLOWED_INFERENCE_MODES: Final = frozenset({"audio"})
_ALLOWED_CREDENTIAL_ENV_NAMES: Final = frozenset({"OPENROUTER_API_KEY"})
_BOOT_ID_PATTERN: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_PRINTABLE_TEXT_PATTERN: Final = re.compile(r"\A[\x20-\x7e\n]*\Z")
_CREDENTIAL_SHAPE_PATTERN: Final = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_HIGH_ENTROPY_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{24,}")
_COLD_IDENTITY_MODEL_CONFIG: Final[ConfigDict] = cast(
    ConfigDict, {**FINITE_NUMERIC_MODEL_CONFIG, "frozen": True, "extra": "forbid"}
)


class ColdIdentityFailure(Enum):
    """Closed failure grammar for frozen cold-run identity admission."""

    MCP_VERSION_NOT_PINNED = "mcp_version_not_pinned"
    TEMPERATURE_UNIT_NOT_CELSIUS = "temperature_unit_not_celsius"
    RECORDING_DEVICE_NOT_SINGLE = "recording_device_not_single"
    INFERENCE_NOT_ACTIVE_IN_IDENTITY = "inference_not_active_in_identity"
    BOOT_ID_UNREADABLE = "boot_id_unreadable"
    BOOT_ID_MALFORMED = "boot_id_malformed"
    CREDENTIAL_NAME_NOT_ALLOWED = "credential_name_not_allowed"
    OPERATOR_TEXT_REJECTED = "operator_text_rejected"


class ColdIdentityError(RuntimeError):
    """Raised when one identity admission fails closed."""

    failure: ColdIdentityFailure

    def __init__(self, failure: ColdIdentityFailure) -> None:
        """Create a closed identity error without embedding uncontrolled values.

        Args:
            failure: The closed reason for refusing the identity.
        """
        super().__init__("Cold identity admission failed.")
        self.failure = failure


class ModelManifestEntry(BaseModel):
    """One packaged-model manifest entry copied from the trusted manifest module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relative_path: str
    sha256: str


class ColdRunIdentity(BaseModel):
    """Immutable complete identity for one cold-characterisation run."""

    model_config = _COLD_IDENTITY_MODEL_CONFIG

    run_id: str
    started_at_utc: str
    agent_version: str
    coffee_roaster_mcp_version: str
    python_version: str
    platform: str
    machine: str
    operating_system: str
    kernel: str
    boot_id: str
    pi_model: str
    pi_revision: str
    runtime_config: RuntimeConfigSnapshot
    server_info: ServerInfo
    device_config: dict[str, object]
    model_repo_id: str
    model_revision: str
    model_manifest: tuple[ModelManifestEntry, ...]
    audio_device_identity: str
    serial_port_path: str
    controller_tick_seconds: float
    pi_evidence_root: str
    laptop_evidence_root: str
    advisor_provider: str
    advisor_model: str
    advisor_prompt_version: str
    credential_env_var_name: str
    credential_present: bool
    stimulus_block: str
    operator_host_notes: str
    operator_psu_notes: str
    operator_cooling_notes: str

    @model_validator(mode="after")
    def _require_packaged_manifest(self) -> ColdRunIdentity:
        """Require packaged constants and closed admissions on every construction path."""
        if _BOOT_ID_PATTERN.fullmatch(self.boot_id) is None:
            raise ColdIdentityError(ColdIdentityFailure.BOOT_ID_MALFORMED)
        _admit_identity_inputs(
            coffee_roaster_mcp_version=self.coffee_roaster_mcp_version,
            runtime_config=self.runtime_config,
            device_config=MCPDeviceConfig.model_validate(self.device_config),
            credential_env_var_name=self.credential_env_var_name,
            operator_texts=(
                self.stimulus_block,
                self.operator_host_notes,
                self.operator_psu_notes,
                self.operator_cooling_notes,
            ),
        )
        expected_entries = tuple(
            ModelManifestEntry(relative_path=item.relative_path, sha256=item.sha256)
            for item in MANIFEST_FILES
        )
        if (
            self.agent_version != __version__
            or self.model_repo_id != REPO_ID
            or self.model_revision != REVISION
            or self.model_manifest != expected_entries
        ):
            raise ValueError("identity must use packaged identity constants")
        if not all(
            math.isfinite(value)
            for value in (
                self.runtime_config.command_interval_seconds,
                self.runtime_config.sample_interval_seconds,
                self.runtime_config.auto_t0_drop_threshold_c,
            )
        ):
            raise ValueError("runtime configuration identity values must be finite")
        return self


def freeze_identity(
    *,
    run_id: str,
    started_at_utc: str,
    coffee_roaster_mcp_version: str,
    python_version: str,
    platform: str,
    machine: str,
    operating_system: str,
    kernel: str,
    pi_model: str,
    pi_revision: str,
    runtime_config: RuntimeConfigSnapshot,
    server_info: ServerInfo,
    device_config: MCPDeviceConfig,
    audio_device_identity: str,
    serial_port_path: str,
    controller_tick_seconds: float,
    pi_evidence_root: str,
    laptop_evidence_root: str,
    advisor_descriptor: AdvisorDescriptor,
    credential_env_var_name: str,
    credential_present: bool,
    stimulus_block: str,
    operator_host_notes: str,
    operator_psu_notes: str,
    operator_cooling_notes: str,
    boot_id_path: Path = BOOT_ID_PATH,
) -> ColdRunIdentity:
    """Freeze a complete admitted cold-run identity without reading a credential.

    Args:
        run_id: The caller-assigned cold session identifier.
        started_at_utc: The caller-recorded UTC start timestamp.
        coffee_roaster_mcp_version: Installed MCP package version.
        python_version: Python implementation version observed by the caller.
        platform: Platform identity observed by the caller.
        machine: Machine architecture identity observed by the caller.
        operating_system: Operating-system identity observed by the caller.
        kernel: Kernel identity observed by the caller.
        pi_model: Raspberry Pi model identity observed by the caller.
        pi_revision: Raspberry Pi revision identity observed by the caller.
        runtime_config: Already-fetched tolerant MCP runtime mirror.
        server_info: Already-fetched tolerant MCP server mirror.
        device_config: Phase-specific managed MCP device configuration.
        audio_device_identity: Primary configured audio-device identity.
        serial_port_path: Configured roaster serial-port path.
        controller_tick_seconds: Controller tick duration.
        pi_evidence_root: Retained-on-Pi evidence root identity.
        laptop_evidence_root: Established laptop evidence root identity.
        advisor_descriptor: Resolved provider/model/prompt descriptor.
        credential_env_var_name: Credential environment-variable name only.
        credential_present: Caller-observed credential presence boolean only.
        stimulus_block: D188 operator stimulus text.
        operator_host_notes: Operator host notes.
        operator_psu_notes: Operator PSU notes.
        operator_cooling_notes: Operator cooling notes.
        boot_id_path: Bounded boot-ID source, injectable solely for hardware-free tests.

    Returns:
        The frozen admitted identity.

    Raises:
        ColdIdentityError: If an explicit identity admission fails closed.
        ValidationError: If the frozen identity model rejects supplied values.
    """
    _admit_identity_inputs(
        coffee_roaster_mcp_version=coffee_roaster_mcp_version,
        runtime_config=runtime_config,
        device_config=device_config,
        credential_env_var_name=credential_env_var_name,
        operator_texts=(
            stimulus_block,
            operator_host_notes,
            operator_psu_notes,
            operator_cooling_notes,
        ),
    )
    boot_id = _read_boot_id(boot_id_path)
    manifest = tuple(
        ModelManifestEntry(relative_path=item.relative_path, sha256=item.sha256)
        for item in MANIFEST_FILES
    )
    return ColdRunIdentity(
        run_id=run_id,
        started_at_utc=started_at_utc,
        agent_version=__version__,
        coffee_roaster_mcp_version=coffee_roaster_mcp_version,
        python_version=python_version,
        platform=platform,
        machine=machine,
        operating_system=operating_system,
        kernel=kernel,
        boot_id=boot_id,
        pi_model=pi_model,
        pi_revision=pi_revision,
        runtime_config=runtime_config,
        server_info=server_info,
        device_config=device_config.model_dump(mode="json"),
        model_repo_id=REPO_ID,
        model_revision=REVISION,
        model_manifest=manifest,
        audio_device_identity=audio_device_identity,
        serial_port_path=serial_port_path,
        controller_tick_seconds=controller_tick_seconds,
        pi_evidence_root=pi_evidence_root,
        laptop_evidence_root=laptop_evidence_root,
        advisor_provider=advisor_descriptor.provider,
        advisor_model=advisor_descriptor.model,
        advisor_prompt_version=advisor_descriptor.prompt_version,
        credential_env_var_name=credential_env_var_name,
        credential_present=credential_present,
        stimulus_block=stimulus_block,
        operator_host_notes=operator_host_notes,
        operator_psu_notes=operator_psu_notes,
        operator_cooling_notes=operator_cooling_notes,
    )


def identity_sha256(identity: ColdRunIdentity) -> str:
    """Return the canonical SHA-256 digest of one frozen identity.

    Args:
        identity: The finite, frozen identity to hash.

    Returns:
        A lowercase hexadecimal SHA-256 digest.
    """
    canonical = json.dumps(
        identity.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _admit_identity_inputs(
    *,
    coffee_roaster_mcp_version: str,
    runtime_config: RuntimeConfigSnapshot,
    device_config: MCPDeviceConfig,
    credential_env_var_name: str,
    operator_texts: tuple[str, ...],
) -> None:
    """Apply the closed non-I/O admissions before freezing any identity bytes."""
    if coffee_roaster_mcp_version != REQUIRED_MCP_VERSION:
        raise ColdIdentityError(ColdIdentityFailure.MCP_VERSION_NOT_PINNED)
    if runtime_config.temperature_unit not in _ALLOWED_CELSIUS_TOKENS:
        raise ColdIdentityError(ColdIdentityFailure.TEMPERATURE_UNIT_NOT_CELSIUS)
    if device_config.recording_devices is None or len(device_config.recording_devices) != 1:
        raise ColdIdentityError(ColdIdentityFailure.RECORDING_DEVICE_NOT_SINGLE)
    if (
        runtime_config.first_crack_mode not in _ALLOWED_INFERENCE_MODES
        or runtime_config.model_precision != "int8"
    ):
        raise ColdIdentityError(ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY)
    if credential_env_var_name not in _ALLOWED_CREDENTIAL_ENV_NAMES:
        raise ColdIdentityError(ColdIdentityFailure.CREDENTIAL_NAME_NOT_ALLOWED)
    for text in operator_texts:
        if not _operator_text_is_safe(text):
            raise ColdIdentityError(ColdIdentityFailure.OPERATOR_TEXT_REJECTED)


def _read_boot_id(path: Path) -> str:
    """Read one bounded, exact Linux boot identifier before decoding it."""
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_BOOT_ID_BYTES + 1)
    except (OSError, ValueError, RuntimeError) as error:
        raise ColdIdentityError(ColdIdentityFailure.BOOT_ID_UNREADABLE) from error
    if len(raw) > _MAX_BOOT_ID_BYTES:
        raise ColdIdentityError(ColdIdentityFailure.BOOT_ID_UNREADABLE)
    try:
        boot_id = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ColdIdentityError(ColdIdentityFailure.BOOT_ID_MALFORMED) from error
    if boot_id.endswith("\n"):
        boot_id = boot_id[:-1]
    if _BOOT_ID_PATTERN.fullmatch(boot_id) is None:
        raise ColdIdentityError(ColdIdentityFailure.BOOT_ID_MALFORMED)
    return boot_id


def _operator_text_is_safe(text: str) -> bool:
    """Check bounded printable text for mechanical credential-shaped content."""
    if len(text) > _MAX_OPERATOR_TEXT_LENGTH or _PRINTABLE_TEXT_PATTERN.fullmatch(text) is None:
        return False
    if _CREDENTIAL_SHAPE_PATTERN.search(text) is not None:
        return False
    return all(_shannon_entropy(token) < 3.5 for token in _HIGH_ENTROPY_TOKEN_PATTERN.findall(text))


def _shannon_entropy(token: str) -> float:
    """Calculate character entropy for one bounded token without external input."""
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in map(token.count, set(token))
    )
