"""Fail-closed frozen identity for cold-characterisation evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Annotated, Final, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    model_validator,
)

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
_REVISION_SECRET_SHAPE_PATTERN: Final = re.compile(
    r"\A(?:"
    r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_-]{16,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|(?:xoxb-|xoxp-|xoxa-|xoxr-|xoxs-)[A-Za-z0-9_-]{16,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}"
    r")\Z"
)
_HIGH_ENTROPY_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{24,}")
_COLD_IDENTITY_MODEL_CONFIG: Final[ConfigDict] = cast(
    ConfigDict, {**FINITE_NUMERIC_MODEL_CONFIG, "frozen": True, "extra": "forbid"}
)
_MANAGED_DEVICE_CONFIG_FIELD_NAMES: Final = frozenset(
    {
        "serial_port",
        "roaster_driver",
        "audio_input_device",
        "recording_enabled",
        "recording_autocapture",
        "recording_devices",
        "fc_mode",
        "fc_confidence_threshold",
        "auto_t0_detection_enabled",
        "auto_t0_drop_threshold_c",
        "mcp_yaml_source_path",
        "ambient_mode",
        "ambient_device",
        "ambient_poll_interval_seconds",
    }
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
    DEVICE_CONFIG_FIELD_SET_DRIFTED = "device_config_field_set_drifted"
    DEVICE_CONFIG_VALUE_REJECTED = "device_config_value_rejected"
    PROVENANCE_ARTEFACT_DIGEST_MISMATCHED = "provenance_artefact_digest_mismatched"


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


class ManagedDeviceIdentity(BaseModel):
    """Frozen closed projection of the managed MCP device configuration."""

    model_config = _COLD_IDENTITY_MODEL_CONFIG

    serial_port: str | None = None
    roaster_driver: str | None = None
    audio_input_device: str | None = None
    recording_enabled: bool | None = None
    recording_autocapture: bool | None = None
    recording_devices: tuple[str, ...] | None = None
    fc_mode: Literal["disabled", "audio", "manual"] | None = None
    fc_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_t0_detection_enabled: bool | None = None
    auto_t0_drop_threshold_c: float | None = Field(default=None, gt=0)
    mcp_yaml_source_path: str | None = None
    ambient_mode: Literal["disabled", "yoctopuce"] | None = None
    ambient_device: str | None = None
    ambient_poll_interval_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _require_safe_string_values(self) -> ManagedDeviceIdentity:
        """Reject unsafe projected device strings before they enter an identity."""
        strings = (
            self.serial_port,
            self.roaster_driver,
            self.audio_input_device,
            self.mcp_yaml_source_path,
            self.ambient_device,
        )
        if any(value is not None and not _managed_device_text_is_safe(value) for value in strings):
            raise ColdIdentityError(ColdIdentityFailure.DEVICE_CONFIG_VALUE_REJECTED)
        if self.recording_devices is not None and any(
            not _managed_device_text_is_safe(value) for value in self.recording_devices
        ):
            raise ColdIdentityError(ColdIdentityFailure.DEVICE_CONFIG_VALUE_REJECTED)
        return self


class ColdArtefactKind(Enum):
    """Closed grammar for the build artefact represented by a cold identity."""

    WHEEL = "wheel"
    SDIST = "sdist"
    EDITABLE_SOURCE = "editable_source"


class AgentBuildProvenance(BaseModel):
    """Caller-supplied immutable build provenance for a cold identity."""

    model_config = _COLD_IDENTITY_MODEL_CONFIG

    source_revision: Annotated[str, Field(strict=True, pattern=r"\A[0-9a-f]{40}\z")]
    source_tree_dirty: Annotated[bool, Field(strict=True)]
    artefact_kind: ColdArtefactKind
    artefact_sha256: Annotated[str, Field(strict=True, pattern=r"\A[0-9a-f]{64}\z")] | None

    @model_validator(mode="after")
    def _require_matching_artefact_digest(self) -> AgentBuildProvenance:
        """Require a digest exactly when the asserted artefact is packaged."""
        packaged = self.artefact_kind in {ColdArtefactKind.WHEEL, ColdArtefactKind.SDIST}
        if packaged == (self.artefact_sha256 is None):
            raise ColdIdentityError(ColdIdentityFailure.PROVENANCE_ARTEFACT_DIGEST_MISMATCHED)
        return self


class EffectiveMCPProfile(BaseModel):
    """Caller-supplied immutable profile commitment and typed comparables."""

    model_config = _COLD_IDENTITY_MODEL_CONFIG

    source_sha256: Annotated[str, Field(strict=True, pattern=r"\A[0-9a-f]{64}\z")]
    source_byte_length: Annotated[int, Field(strict=True, ge=0)]
    first_crack_onnx_threads: Annotated[int, Field(strict=True, ge=1)]
    first_crack_min_positive_windows: Annotated[int, Field(strict=True, ge=1)]
    first_crack_confirmation_window_seconds: Annotated[float, Field(strict=True, gt=0)]
    first_crack_revision: Annotated[str, Field(strict=True, pattern=r"\A[A-Za-z0-9._-]{1,128}\z")]
    audio_sample_rate: Annotated[int, Field(strict=True, gt=0)]
    audio_window_seconds: Annotated[float, Field(strict=True, gt=0)]
    audio_overlap: Annotated[float, Field(strict=True, ge=0.0, lt=1.0)]
    audio_hop_seconds: Annotated[float, Field(strict=True, gt=0)] | None
    session_ror_window_seconds: Annotated[int, Field(strict=True, gt=0)]
    session_ror_min_sample_seconds: Annotated[int, Field(strict=True, gt=0)]

    @model_validator(mode="wrap")
    @classmethod
    def _reject_credential_shaped_revision(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[EffectiveMCPProfile],
    ) -> EffectiveMCPProfile:
        """Reject credential-shaped operator YAML revisions without entropy screening."""
        raw_revision: object | None = (
            cast(Mapping[str, object], value).get("first_crack_revision")
            if isinstance(value, Mapping)
            else None
        )
        try:
            profile = handler(value)
        except ValidationError:
            if isinstance(raw_revision, str) and _revision_has_credential_shape(raw_revision):
                raise ColdIdentityError(ColdIdentityFailure.OPERATOR_TEXT_REJECTED) from None
            raise
        if _revision_has_credential_shape(profile.first_crack_revision):
            raise ColdIdentityError(ColdIdentityFailure.OPERATOR_TEXT_REJECTED)
        return profile


def _revision_has_credential_shape(revision: str) -> bool:
    """Return whether an MCP revision resembles a credential rather than a revision."""
    return (
        _CREDENTIAL_SHAPE_PATTERN.search(revision) is not None
        or _REVISION_SECRET_SHAPE_PATTERN.fullmatch(revision) is not None
    )


class ColdRunIdentity(BaseModel):
    """Immutable recorded identity assertions for one cold-characterisation run."""

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
    device_config: ManagedDeviceIdentity
    build_provenance: AgentBuildProvenance
    effective_mcp_profile: EffectiveMCPProfile
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
            device_config=self.device_config,
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
    build_provenance: AgentBuildProvenance,
    effective_mcp_profile: EffectiveMCPProfile,
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
    """Freeze a caller-supplied admitted cold-run identity without reading a credential.

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
        build_provenance: Caller-supplied immutable build provenance assertion.
        effective_mcp_profile: Caller-supplied effective MCP configuration commitment.
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
    managed_device_config = _project_managed_device_config(device_config)
    _admit_identity_inputs(
        coffee_roaster_mcp_version=coffee_roaster_mcp_version,
        runtime_config=runtime_config,
        device_config=managed_device_config,
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
        device_config=managed_device_config,
        build_provenance=build_provenance,
        effective_mcp_profile=effective_mcp_profile,
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
    device_config: ManagedDeviceIdentity,
    credential_env_var_name: str,
    operator_texts: tuple[str, ...],
) -> None:
    """Apply the closed non-I/O admissions before freezing any identity bytes."""
    _require_mcp_device_config_field_set()
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


def _require_mcp_device_config_field_set() -> None:
    """Refuse to freeze when the upstream managed field grammar has drifted."""
    if frozenset(MCPDeviceConfig.model_fields) != _MANAGED_DEVICE_CONFIG_FIELD_NAMES:
        raise ColdIdentityError(ColdIdentityFailure.DEVICE_CONFIG_FIELD_SET_DRIFTED)


def _project_managed_device_config(device_config: MCPDeviceConfig) -> ManagedDeviceIdentity:
    """Copy every managed device field into the frozen closed identity grammar."""
    _require_mcp_device_config_field_set()
    source_path = device_config.mcp_yaml_source_path
    return ManagedDeviceIdentity(
        serial_port=device_config.serial_port,
        roaster_driver=device_config.roaster_driver,
        audio_input_device=device_config.audio_input_device,
        recording_enabled=device_config.recording_enabled,
        recording_autocapture=device_config.recording_autocapture,
        recording_devices=device_config.recording_devices,
        fc_mode=device_config.fc_mode,
        fc_confidence_threshold=device_config.fc_confidence_threshold,
        auto_t0_detection_enabled=device_config.auto_t0_detection_enabled,
        auto_t0_drop_threshold_c=device_config.auto_t0_drop_threshold_c,
        mcp_yaml_source_path=str(source_path) if source_path is not None else None,
        ambient_mode=device_config.ambient_mode,
        ambient_device=device_config.ambient_device,
        ambient_poll_interval_seconds=device_config.ambient_poll_interval_seconds,
    )


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


def _managed_device_text_is_safe(text: str) -> bool:
    """Check bounded device identity text without applying operator-note entropy rules."""
    return (
        len(text) <= 512
        and _PRINTABLE_TEXT_PATTERN.fullmatch(text) is not None
        and _CREDENTIAL_SHAPE_PATTERN.search(text) is None
    )


def _shannon_entropy(token: str) -> float:
    """Calculate character entropy for one bounded token without external input."""
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in map(token.count, set(token))
    )
