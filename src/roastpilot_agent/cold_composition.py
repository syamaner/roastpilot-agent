"""Cold-run composition: one admitted child, client, advisor and runtime call (#954 U1).

This infrastructure adapter turns the two-phase cold runtime's ports into one real
``coffee-roaster-mcp`` child whose environment, configuration bytes and advisor are
admitted before any process object is constructed.  Every refusal is a closed
:class:`ColdCompositionRefusal` member; no raw exception text escapes.  The
runtime's result is returned exactly as produced, with no intervening await,
cleanup or output.

Boundaries (stated, not resolved here): the advisor credential is never injected
into the child's environment, but an operator's MCP YAML may itself contain
private settings, so no rendered file is claimed credential-free.  This module
makes no inference-readiness, detector, hardware or acceptance claim.  Temperatures
are Celsius throughout.
"""

from __future__ import annotations

import enum
import hashlib
import os
import re
import secrets
import stat
import tempfile
import typing
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, TracebackType

import mcp.client.stdio as _sdk_stdio
import pydantic
import yaml
from mcp import StdioServerParameters
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken

from roastpilot_agent.advisor import AdvisorDescriptor, AdvisorUsage, RoastAdvisor
from roastpilot_agent.cold_characterisation.advisory_sampler import (
    ColdAdvisoryAdvisorPort,
    ColdAdvisorySpec,
)
from roastpilot_agent.cold_characterisation.engine import ColdEngineClock, ColdEngineHost
from roastpilot_agent.cold_characterisation.engine_policy import (
    COLD_OBSERVATION_INTERVAL_SECONDS,
)
from roastpilot_agent.cold_characterisation.evidence_lifecycle import is_admissible_utc_instant
from roastpilot_agent.cold_characterisation.evidence_schema import ColdPhaseKind
from roastpilot_agent.cold_characterisation.evidence_store import (
    ColdAdmittedRoot,
    admit_evidence_root,
)
from roastpilot_agent.cold_characterisation.identity import (
    REQUIRED_MCP_VERSION,
    AgentBuildProvenance,
    ColdRunIdentity,
    EffectiveMCPProfile,
    freeze_identity,
)
from roastpilot_agent.cold_characterisation.mcp import ColdCharacterisationMCPClient
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildLifecycle,
    ColdPhaseIdentitySource,
    ColdTwoPhaseResult,
    run_two_phase_characterisation,
)
from roastpilot_agent.config import AppConfig, MCPConfig, MCPDeviceConfig
from roastpilot_agent.live import build_advisor
from roastpilot_agent.mcp_client import MCPServerProcess, resolve_mcp_command
from roastpilot_agent.mcp_yaml import render_mcp_yaml
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyPolicy

__all__ = (
    "CHILD_CONFIG_ENV_NAME",
    "MAX_COLD_MCP_RENDERED_YAML_BYTES",
    "MAX_COLD_MCP_SOURCE_YAML_BYTES",
    "MAX_COLD_MCP_YAML_DEPTH",
    "MAX_COLD_MCP_YAML_NODES",
    "ColdCompositionChildError",
    "ColdCompositionInputs",
    "ColdCompositionRefusal",
    "ColdCompositionResources",
    "ColdHostFacts",
    "ColdIdentitySource",
    "ColdMCPChild",
    "ColdMCPServerProcess",
    "random_run_suffix",
    "run_cold_characterisation",
)

#: Ordinary software resource limits for the operator's MCP YAML (not hardware limits).
MAX_COLD_MCP_SOURCE_YAML_BYTES: typing.Final = 65_536
#: Ordinary software resource limit for each rendered phase YAML.
MAX_COLD_MCP_RENDERED_YAML_BYTES: typing.Final = 131_072
#: Maximum nesting depth of an admitted YAML document (the root is depth 1).
MAX_COLD_MCP_YAML_DEPTH: typing.Final = 32
#: Maximum number of value nodes (the root included) of an admitted YAML document.
MAX_COLD_MCP_YAML_NODES: typing.Final = 4_096
#: The single ``COFFEE_*`` key the cold child receives: its bound config path.
CHILD_CONFIG_ENV_NAME: typing.Final = "COFFEE_ROASTER_MCP_CONFIG"

_POSIX: typing.Final = "posix"
_NT: typing.Final = "nt"
#: R3 execution allow-list per ``os.name`` family (values copied from one snapshot).
_EXEC_ALLOW: typing.Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        _POSIX: frozenset(
            {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"}
        ),
        _NT: frozenset(
            {
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
                "USERNAME",
            }
        ),
    }
)
#: The MCP SDK 1.30.0 default-inherited names, pinned per family.  The SDK merges
#: these from the live process environment under ``server.env``; every one is
#: therefore set explicitly in the frozen child mapping.
_SDK_DEFAULT: typing.Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        _POSIX: frozenset({"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}),
        _NT: frozenset(
            {
                "APPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
                "LOCALAPPDATA",
                "PATH",
                "PATHEXT",
                "PROCESSOR_ARCHITECTURE",
                "SYSTEMDRIVE",
                "SYSTEMROOT",
                "TEMP",
                "USERNAME",
                "USERPROFILE",
            }
        ),
    }
)
#: Present, non-empty, non-function values required at the snapshot.
_REQUIRED: typing.Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {family: _SDK_DEFAULT[family] & _EXEC_ALLOW[family] for family in (_POSIX, _NT)}
)
#: SDK default names outside the allow-list, always set to the empty string.
_NEUTRALISED: typing.Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {family: _SDK_DEFAULT[family] - _EXEC_ALLOW[family] for family in (_POSIX, _NT)}
)
#: Allow-listed names copied only when present.
_OPTIONAL: typing.Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {family: _EXEC_ALLOW[family] - _SDK_DEFAULT[family] for family in (_POSIX, _NT)}
)
#: Upper-cased names a credential variable may never use (both families + config key).
_COLLISION_NAMES: typing.Final = frozenset(
    name.upper() for family in (_POSIX, _NT) for name in _EXEC_ALLOW[family] | _SDK_DEFAULT[family]
) | {CHILD_CONFIG_ENV_NAME}
_FUNCTION_VALUE_PREFIX: typing.Final = "()"
_ENV_REFUSED: typing.Final = "Cold child environment refused."
_COFFEE_PREFIX: typing.Final = "COFFEE_"
_RUN_SUFFIX_PATTERN: typing.Final = re.compile(r"[a-z0-9-]{1,48}")
_REFUSED_YAML_TOKENS: typing.Final = (AliasToken, AnchorToken, TagToken, DirectiveToken)
#: ``(section, key, EffectiveMCPProfile field)`` for the ten pinned 0.2.2 comparables.
_PROFILE_KEYS: typing.Final = (
    ("first_crack", "onnx_threads", "first_crack_onnx_threads"),
    ("first_crack", "min_positive_windows", "first_crack_min_positive_windows"),
    ("first_crack", "confirmation_window_seconds", "first_crack_confirmation_window_seconds"),
    ("first_crack", "revision", "first_crack_revision"),
    ("audio", "sample_rate", "audio_sample_rate"),
    ("audio", "window_seconds", "audio_window_seconds"),
    ("audio", "overlap", "audio_overlap"),
    ("audio", "hop_seconds", "audio_hop_seconds"),
    ("session", "ror_window_seconds", "session_ror_window_seconds"),
    ("session", "ror_min_sample_seconds", "session_ror_min_sample_seconds"),
)
_SOURCE_NAME: typing.Final = "source.yaml"
_ACTIVE_NAME: typing.Final = "active.yaml"
_STAGING_NAME: typing.Final = "active.yaml.next"
_RENDERED_NAMES: typing.Final[Mapping[ColdPhaseKind, str]] = MappingProxyType(
    {ColdPhaseKind.RECORDING_OFF: "phase-off.yaml", ColdPhaseKind.RECORDING_ON: "phase-on.yaml"}
)
_PRIVATE_FILE_MODE: typing.Final = 0o600
_INPUT_MODEL_CONFIG: typing.Final = pydantic.ConfigDict(frozen=True, extra="forbid", strict=True)

_NonEmpty = typing.Annotated[str, pydantic.Field(min_length=1)]


class ColdCompositionRefusal(enum.Enum):
    """Closed pre-construction refusal, in check order; carries no raw detail."""

    RESOURCES_NOT_ADMITTED = "resources_not_admitted"
    MCP_ENV_NOT_ADMITTED = "mcp_env_not_admitted"
    CREDENTIAL_ABSENT = "credential_absent"
    ADVISOR_BUILD_FAILED = "advisor_build_failed"
    ADVISOR_UNAVAILABLE = "advisor_unavailable"
    ADVISOR_NOT_ADMITTED = "advisor_not_admitted"
    DEVICE_CONFIG_NOT_ADMITTED = "device_config_not_admitted"
    RUN_ID_NOT_ADMITTED = "run_id_not_admitted"
    ROOT_NOT_ADMITTED = "root_not_admitted"
    MCP_SOURCE_NOT_ADMITTED = "mcp_source_not_admitted"
    PROFILE_NOT_ADMITTED = "profile_not_admitted"


class ColdCompositionChildError(RuntimeError):
    """A fixed-message refusal from the cold child adapter."""


class ColdHostFacts(pydantic.BaseModel):
    """Caller-observed host identity facts; the production reader is a later unit's."""

    model_config = _INPUT_MODEL_CONFIG

    coffee_roaster_mcp_version: _NonEmpty
    python_version: _NonEmpty
    platform: _NonEmpty
    machine: _NonEmpty
    operating_system: _NonEmpty
    kernel: _NonEmpty
    pi_model: _NonEmpty
    pi_revision: _NonEmpty
    boot_id_path: Path


class ColdCompositionInputs(pydantic.BaseModel):
    """Explicit caller inputs for one cold run; nothing here is defaulted or inferred."""

    model_config = _INPUT_MODEL_CONFIG

    spec: ColdAdvisorySpec
    build_provenance: AgentBuildProvenance
    host_facts: ColdHostFacts
    device_config: MCPDeviceConfig
    pi_evidence_root: str
    laptop_evidence_root: str
    protected_roots: tuple[str, ...]
    audio_device_identity: str
    serial_port_path: str
    stimulus_block: str
    operator_host_notes: str
    operator_psu_notes: str
    operator_cooling_notes: str


class ColdCompositionResources:
    """Caller-owned private temporary directory for the snapshot and rendered YAML.

    The directory is created with mode 0700 on entry and removed on exit; exit is
    the only cleanup.  :attr:`directory` is ``None`` until entered and after exit.
    Instances are single-use.
    """

    def __init__(self) -> None:
        """Create unentered resources."""
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._directory: Path | None = None
        self._used = False

    @property
    def directory(self) -> Path | None:
        """The private directory while entered, else ``None``."""
        return self._directory

    def __enter__(self) -> typing.Self:
        """Create the private directory.

        Returns:
            These resources.

        Raises:
            ColdCompositionChildError: If the instance was already entered.
        """
        if self._used:
            raise ColdCompositionChildError("Cold composition resources refused.")
        self._used = True
        self._temporary = tempfile.TemporaryDirectory(prefix="roastpilot-cold-")
        self._directory = Path(self._temporary.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Remove the private directory and everything in it.

        Args:
            exc_type: The exception type, if any.
            exc_value: The exception, if any.
            traceback: The traceback, if any.
        """
        del exc_type, exc_value, traceback
        temporary, self._temporary, self._directory = self._temporary, None, None
        if temporary is not None:
            temporary.cleanup()


class _EnvSnapshot(typing.NamedTuple):
    """One frozen read of the process environment; the credential value is never kept."""

    coffee_names_present: bool
    values: Mapping[str, str]
    credential_present: bool


def _admit_sdk_default_names(raw: object, family: str) -> bool:
    """Admit the SDK's current default-name list against the pinned family table.

    Args:
        raw: A read-only reference to the SDK's current module constant.
        family: The ``os.name`` platform family.

    Returns:
        ``True`` only for an exact ``list`` or ``tuple`` of exact, unique ``str``
        names, all within the pinned table (a strict subset is admissible).
    """
    pinned = _SDK_DEFAULT.get(family)
    if pinned is None or (type(raw) is not list and type(raw) is not tuple):
        return False
    items = tuple(typing.cast("list[object] | tuple[object, ...]", raw))
    if not all(type(item) is str for item in items):
        return False
    names = typing.cast("tuple[str, ...]", items)
    return len(set(names)) == len(names) and set(names) <= pinned


def _credential_name_collides(api_key_env: str) -> bool:
    """Whether the credential variable name could enter the child mapping.

    Case-insensitive, across both family tables, the config key and any
    ``COFFEE_*`` name.

    Args:
        api_key_env: The configured credential variable name.

    Returns:
        ``True`` when the name is refused.
    """
    name = api_key_env.upper()
    return name in _COLLISION_NAMES or name.startswith(_COFFEE_PREFIX)


def _snapshot_environment(
    source: Mapping[str, str], api_key_env: str, family: str
) -> _EnvSnapshot | None:
    """Snapshot names, admitted values and credential presence; ``None`` for an unknown family.

    Only the values of the required and optional execution names are read; the
    credential is inspected for truthiness only and never kept.

    Args:
        source: The environment mapping.
        api_key_env: The configured credential variable name.
        family: The ``os.name`` platform family.

    Returns:
        The frozen snapshot, or ``None`` when the family has no tables.
    """
    if family not in _EXEC_ALLOW:
        return None
    names = tuple(source)
    wanted = _REQUIRED[family] | _OPTIONAL[family]
    values = {name: source[name] for name in names if name in wanted}
    return _EnvSnapshot(
        coffee_names_present=any(name.upper().startswith(_COFFEE_PREFIX) for name in names),
        values=MappingProxyType(values),
        credential_present=bool(source.get(api_key_env)),
    )


def _read_process_environment(api_key_env: str, family: str) -> _EnvSnapshot | None:
    """The module's single process-environment access site.

    Args:
        api_key_env: The configured credential variable name.
        family: The ``os.name`` platform family.

    Returns:
        The frozen snapshot, or ``None`` when the family has no tables.
    """
    return _snapshot_environment(os.environ, api_key_env, family)


def _admit_values(values: Mapping[str, str], family: str) -> bool:
    """Required values present, non-empty and not function-valued; optional not function-valued.

    Args:
        values: The snapshot values.
        family: A known platform family.

    Returns:
        Whether the values are admitted (an absent required name is never filled).
    """
    required = all(
        name in values and values[name] and not values[name].startswith(_FUNCTION_VALUE_PREFIX)
        for name in _REQUIRED[family]
    )
    return required and not any(
        values[name].startswith(_FUNCTION_VALUE_PREFIX)
        for name in _OPTIONAL[family]
        if name in values
    )


def _cold_child_environment(
    values: Mapping[str, str], family: str, config_path: Path
) -> Mapping[str, str]:
    """The closed, frozen child mapping: every SDK default name set explicitly.

    Args:
        values: Admitted snapshot values.
        family: A known platform family.
        config_path: The installed active YAML path.

    Returns:
        A read-only mapping of required, present optional, neutralised (``""``)
        names and the config path; nothing else.
    """
    return MappingProxyType(
        {
            **{name: values[name] for name in _REQUIRED[family]},
            **{name: values[name] for name in _OPTIONAL[family] if name in values},
            **dict.fromkeys(_NEUTRALISED[family], ""),
            CHILD_CONFIG_ENV_NAME: str(config_path),
        }
    )


def _admit_child_environment(config: MCPConfig, environment: object, family: str) -> bool:
    """The constructor's closed admission of a frozen child mapping.

    Args:
        config: The MCP child settings; ``env`` must be empty.
        environment: The candidate child mapping.
        family: The ``os.name`` platform family.

    Returns:
        Whether the mapping is exactly admissible for ``family``.
    """
    if (
        config.env != {}
        or family not in _EXEC_ALLOW
        or not isinstance(environment, Mapping)
        or not _admit_sdk_default_names(_sdk_stdio.DEFAULT_INHERITED_ENV_VARS, family)
    ):
        return False
    items = tuple(typing.cast(Mapping[object, object], environment).items())
    if not all(type(key) is str and type(value) is str for key, value in items):
        return False
    mapping = typing.cast(Mapping[str, str], dict(items))
    names = set(mapping)
    coffee = [name for name in names if name.upper().startswith(_COFFEE_PREFIX)]
    return (
        _SDK_DEFAULT[family] | {CHILD_CONFIG_ENV_NAME} <= names
        and names <= _EXEC_ALLOW[family] | _SDK_DEFAULT[family] | {CHILD_CONFIG_ENV_NAME}
        and coffee == [CHILD_CONFIG_ENV_NAME]
        and bool(mapping[CHILD_CONFIG_ENV_NAME])
        and all(mapping[name] == "" for name in _NEUTRALISED[family])
        and _admit_values(mapping, family)
    )


def _admit_environment(config: AppConfig, family: str) -> _EnvSnapshot | ColdCompositionRefusal:
    """Step 2: family, SDK list, credential name, config env, snapshot, ``COFFEE_*``, values."""
    try:
        if (
            family not in _EXEC_ALLOW
            or not _admit_sdk_default_names(_sdk_stdio.DEFAULT_INHERITED_ENV_VARS, family)
            or _credential_name_collides(config.advisor.api_key_env)
            or config.mcp.env != {}
        ):
            return ColdCompositionRefusal.MCP_ENV_NOT_ADMITTED
        snapshot = _read_process_environment(config.advisor.api_key_env, family)
        if (
            snapshot is None
            or snapshot.coffee_names_present
            or not _admit_values(snapshot.values, family)
        ):
            return ColdCompositionRefusal.MCP_ENV_NOT_ADMITTED
        return snapshot
    except Exception:
        return ColdCompositionRefusal.MCP_ENV_NOT_ADMITTED


def _admit_advisor(
    builder: Callable[[AppConfig], RoastAdvisor | None], config: AppConfig
) -> tuple[ColdAdvisoryAdvisorPort, AdvisorDescriptor] | ColdCompositionRefusal:
    """Step 4: build once, then admit against the sampler's port, not a provider type."""
    try:
        advisor = builder(config)
    except Exception:
        return ColdCompositionRefusal.ADVISOR_BUILD_FAILED
    if advisor is None:
        return ColdCompositionRefusal.ADVISOR_UNAVAILABLE
    try:
        descriptor: object = advisor.descriptor_for(RoastPhase.PREHEATING)
        usage: object = getattr(advisor, "last_usage")  # noqa: B009 - port member read
        recommend: object = getattr(advisor, "get_recommendation", None)
    except Exception:
        return ColdCompositionRefusal.ADVISOR_NOT_ADMITTED
    if (
        type(descriptor) is not AdvisorDescriptor
        or not all(
            type(value) is str and value
            for value in (descriptor.provider, descriptor.model, descriptor.prompt_version)
        )
        or not (usage is None or isinstance(usage, AdvisorUsage))
        or not callable(recommend)
    ):
        return ColdCompositionRefusal.ADVISOR_NOT_ADMITTED
    return typing.cast(ColdAdvisoryAdvisorPort, advisor), descriptor


def _admit_device_config(
    base: MCPDeviceConfig,
) -> Mapping[ColdPhaseKind, MCPDeviceConfig] | ColdCompositionRefusal:
    """Step 5: derive the two phase configs from a recording-unset base."""
    try:
        source = base.mcp_yaml_source_path
        if (
            base.recording_enabled is not None
            or base.recording_autocapture is not None
            or source is None
            or not source.is_absolute()
        ):
            return ColdCompositionRefusal.DEVICE_CONFIG_NOT_ADMITTED
        return MappingProxyType(
            {
                ColdPhaseKind.RECORDING_OFF: base.model_copy(
                    update={"recording_enabled": False, "recording_autocapture": False}
                ),
                ColdPhaseKind.RECORDING_ON: base.model_copy(
                    update={"recording_enabled": True, "recording_autocapture": True}
                ),
            }
        )
    except Exception:
        return ColdCompositionRefusal.DEVICE_CONFIG_NOT_ADMITTED


def random_run_suffix() -> str:
    """Return a fresh lowercase-hex run-ID suffix.

    Returns:
        Sixteen lowercase hexadecimal characters.
    """
    return secrets.token_hex(8)


def _mint_run_id(
    clock: ColdEngineClock, run_suffix: Callable[[], str]
) -> tuple[str, str] | ColdCompositionRefusal:
    """Step 6: mint the single run ID shared by both phases, and its UTC start."""
    try:
        started: object = clock.utc_now_iso()
        if type(started) is not str or not is_admissible_utc_instant(started):
            return ColdCompositionRefusal.RUN_ID_NOT_ADMITTED
        instant = datetime.fromisoformat(started)
        if instant.utcoffset() != timedelta(0):  # pragma: no cover - admitted above
            return ColdCompositionRefusal.RUN_ID_NOT_ADMITTED
        suffix: object = run_suffix()
        if type(suffix) is not str or _RUN_SUFFIX_PATTERN.fullmatch(suffix) is None:
            return ColdCompositionRefusal.RUN_ID_NOT_ADMITTED
        return f"{instant:%Y%m%dT%H%M%SZ}-{suffix}", started
    except Exception:
        return ColdCompositionRefusal.RUN_ID_NOT_ADMITTED


def _admit_root(inputs: ColdCompositionInputs) -> ColdAdmittedRoot | ColdCompositionRefusal:
    """Step 7: admit the evidence root through the existing store admission."""
    try:
        return admit_evidence_root(inputs.pi_evidence_root, protected_roots=inputs.protected_roots)
    except Exception:
        return ColdCompositionRefusal.ROOT_NOT_ADMITTED


def _read_bounded_regular(path: Path, limit: int) -> bytes:
    """Read one regular file once through a no-follow, non-blocking descriptor.

    The file must be a regular file both before the open and on the opened
    descriptor, with the same identity; a symlink, FIFO, device or swapped target
    is refused without reading.  Fails closed where no-follow is unavailable.

    Args:
        path: The file to read.
        limit: The maximum admitted byte count.

    Returns:
        The bytes read.

    Raises:
        ColdCompositionChildError: If any admission check fails.
        OSError: If the file cannot be inspected, opened or read.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or nonblock is None:  # pragma: no cover - non-POSIX
        raise ColdCompositionChildError("Cold file read refused.")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ColdCompositionChildError("Cold file read refused.")
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_NOCTTY", 0)
    descriptor = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ColdCompositionChildError("Cold file read refused.")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ColdCompositionChildError("Cold file read refused.")
    return data


def _write_private(path: Path, data: bytes, *, exclusive: bool) -> None:
    """Write ``data`` to a private (0600) file without following a symlink.

    Args:
        path: The destination inside the private resources directory.
        data: The bytes to write.
        exclusive: Refuse an existing file when ``True``; truncate it otherwise.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
    finally:
        os.close(descriptor)


def _admit_yaml(data: bytes) -> dict[str, object]:
    """Decode, pre-scan and bound one YAML document; return its mapping root.

    Args:
        data: The bounded bytes.

    Returns:
        The parsed mapping.

    Raises:
        ColdCompositionChildError: On an alias, anchor, tag, directive, non-mapping
            root, non-``str`` key, or a depth or node bound breach.
        UnicodeDecodeError: On non-UTF-8 bytes.
        yaml.YAMLError: On a malformed document.
    """
    text = data.decode("utf-8")
    tokens = typing.cast(
        Iterator[object],
        yaml.scan(text, Loader=yaml.SafeLoader),  # pyright: ignore[reportUnknownMemberType]
    )
    for token in tokens:
        if isinstance(token, _REFUSED_YAML_TOKENS):
            raise ColdCompositionChildError("Cold YAML refused.")
    loaded: object = yaml.safe_load(text)
    if type(loaded) is not dict:
        raise ColdCompositionChildError("Cold YAML refused.")
    stack: list[tuple[object, int]] = [(loaded, 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_COLD_MCP_YAML_NODES or depth > MAX_COLD_MCP_YAML_DEPTH:
            raise ColdCompositionChildError("Cold YAML refused.")
        if isinstance(node, dict):
            for key, value in typing.cast(dict[object, object], node).items():
                if type(key) is not str:
                    raise ColdCompositionChildError("Cold YAML refused.")
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in typing.cast(list[object], node))
    return typing.cast(dict[str, object], loaded)


class _RenderedPhase(typing.NamedTuple):
    """One phase's committed rendered bytes and their parsed mapping."""

    data: bytes
    parsed: dict[str, object]


def _snapshot_and_render(
    directory: Path, configs: Mapping[ColdPhaseKind, MCPDeviceConfig]
) -> Mapping[ColdPhaseKind, _RenderedPhase] | ColdCompositionRefusal:
    """Step 8a: snapshot the operator source once, then render both phases from it."""
    try:
        operator_source = configs[ColdPhaseKind.RECORDING_OFF].mcp_yaml_source_path
        if operator_source is None:  # pragma: no cover - refused at step 5
            return ColdCompositionRefusal.MCP_SOURCE_NOT_ADMITTED
        source = _read_bounded_regular(operator_source, MAX_COLD_MCP_SOURCE_YAML_BYTES)
        _admit_yaml(source)
        snapshot = directory / _SOURCE_NAME
        _write_private(snapshot, source, exclusive=True)
        rendered: dict[ColdPhaseKind, _RenderedPhase] = {}
        for phase, name in _RENDERED_NAMES.items():
            destination = directory / name
            render_mcp_yaml(configs[phase], snapshot, destination)
            data = _read_bounded_regular(destination, MAX_COLD_MCP_RENDERED_YAML_BYTES)
            rendered[phase] = _RenderedPhase(data, _admit_yaml(data))
        return MappingProxyType(rendered)
    except Exception:
        return ColdCompositionRefusal.MCP_SOURCE_NOT_ADMITTED


def _effective_profile(rendered: _RenderedPhase) -> EffectiveMCPProfile:
    """Project the ten comparables unchanged into a strict profile; no defaults.

    Args:
        rendered: One phase's committed bytes and parsed mapping.

    Returns:
        The strict profile.

    Raises:
        KeyError: On a missing section or key.
        ColdCompositionChildError: On a non-mapping section.
        pydantic.ValidationError: On a type or value refusal.
    """
    values: dict[str, object] = {
        "source_sha256": hashlib.sha256(rendered.data).hexdigest(),
        "source_byte_length": len(rendered.data),
    }
    for section, key, field in _PROFILE_KEYS:
        block = rendered.parsed[section]
        if type(block) is not dict:
            raise ColdCompositionChildError("Cold profile refused.")
        values[field] = typing.cast(dict[str, object], block)[key]
    return EffectiveMCPProfile.model_validate(values, strict=True)


def _admit_profiles(
    rendered: Mapping[ColdPhaseKind, _RenderedPhase],
) -> Mapping[ColdPhaseKind, EffectiveMCPProfile] | ColdCompositionRefusal:
    """Step 8b: admit one strict effective profile per phase."""
    try:
        return MappingProxyType(
            {phase: _effective_profile(data) for phase, data in rendered.items()}
        )
    except Exception:
        return ColdCompositionRefusal.PROFILE_NOT_ADMITTED


class ColdMCPServerProcess(MCPServerProcess):
    """The cold child process: a closed, frozen environment at the spawn-parameter seam.

    Only construction and :meth:`build_server_parameters` differ from the base;
    start, stop, the native spawn factory, process-group ownership, timeouts and
    the sticky unconfirmed-stop flag are inherited unchanged.  The base never
    renders YAML for this process.
    """

    def __init__(
        self, config: MCPConfig, *, child_environment: Mapping[str, str], family: str
    ) -> None:
        """Validate and freeze the closed child mapping.

        Args:
            config: The MCP child settings; ``env`` must be empty.
            child_environment: Required, optional, neutralised and config-path names.
            family: The ``os.name`` platform family the mapping was built for.

        Raises:
            ValueError: If the configuration, SDK list or mapping is not admitted.
        """
        if not _admit_child_environment(config, child_environment, family):
            raise ValueError(_ENV_REFUSED)
        self._cold_family = family
        self._cold_command = config.command
        self._cold_environment: Mapping[str, str] = MappingProxyType(dict(child_environment))
        super().__init__(config, device_config=None)

    def build_server_parameters(self) -> StdioServerParameters:
        """Return ``<command> serve`` with a fresh copy of the frozen mapping.

        The SDK's current default-name list is re-admitted on every spawn, before
        the SDK is called; a refusal raises and the start fails with no spawn.  The
        child environment is the frozen mapping only: it is never derived from the
        live process environment at spawn.  The command goes through the existing
        resolver unchanged, whose accepted default-command fallback may consult the
        live ``PATH`` to locate the binary; that is not a child-environment input.

        Returns:
            The stdio spawn parameters, identical for both phases.

        Raises:
            ValueError: If the SDK's default-name list is no longer admitted.
        """
        if not _admit_sdk_default_names(_sdk_stdio.DEFAULT_INHERITED_ENV_VARS, self._cold_family):
            raise ValueError(_ENV_REFUSED)
        return StdioServerParameters(
            command=resolve_mcp_command(self._cold_command),
            args=["serve"],
            env=dict(self._cold_environment),
        )


class ColdMCPChild:
    """:class:`ColdChildLifecycle` over one cold process and two committed configurations."""

    def __init__(
        self,
        process: MCPServerProcess,
        *,
        directory: Path,
        rendered: Mapping[ColdPhaseKind, bytes],
    ) -> None:
        """Bind the one process and the committed phase bytes.

        Args:
            process: The single cold child process.
            directory: The private resources directory.
            rendered: The committed rendered bytes per phase.
        """
        self._process = process
        self._active = directory / _ACTIVE_NAME
        self._staging = directory / _STAGING_NAME
        self._rendered: Mapping[ColdPhaseKind, bytes] = MappingProxyType(dict(rendered))

    def configure_phase(self, phase: ColdPhaseKind) -> None:
        """Install the committed bytes for ``phase`` as the active YAML; no MCP call.

        Args:
            phase: The phase whose bytes the next spawn reads.

        Raises:
            ColdCompositionChildError: If the child is running or the installed
                bytes differ from the committed bytes.
        """
        if self._process.running:
            raise ColdCompositionChildError("Cold child configuration refused.")
        committed = self._rendered[phase]
        _write_private(self._staging, committed, exclusive=False)
        os.replace(self._staging, self._active)
        installed = _read_bounded_regular(self._active, MAX_COLD_MCP_RENDERED_YAML_BYTES)
        if installed != committed:
            raise ColdCompositionChildError("Cold child configuration refused.")

    async def start(self) -> None:
        """Start the one process."""
        await self._process.start()

    async def stop(self) -> None:
        """Stop the one process."""
        await self._process.stop()

    @property
    def running(self) -> bool:
        """Whether the process has a session attached."""
        return self._process.running

    @property
    def stop_unconfirmed(self) -> bool:
        """The process's sticky unconfirmed-stop flag."""
        return self._process.stop_unconfirmed


class ColdIdentitySource:
    """:class:`ColdPhaseIdentitySource` freezing from same-client reads and explicit inputs."""

    def __init__(
        self,
        *,
        client: ColdCharacterisationMCPClient,
        inputs: ColdCompositionInputs,
        run_id: str,
        started_at_utc: str,
        device_configs: Mapping[ColdPhaseKind, MCPDeviceConfig],
        profiles: Mapping[ColdPhaseKind, EffectiveMCPProfile],
        root: ColdAdmittedRoot,
        descriptor: AdvisorDescriptor,
        credential_env_var_name: str,
        credential_present: bool,
        expected_config_source: str,
    ) -> None:
        """Bind the identity inputs.

        Args:
            client: The single cold client.
            inputs: The explicit caller inputs.
            run_id: The single minted run ID.
            started_at_utc: The admitted UTC start instant.
            device_configs: The per-phase device configs.
            profiles: The per-phase effective profiles.
            root: The admitted evidence root.
            descriptor: The admitted advisor descriptor.
            credential_env_var_name: The credential variable name only.
            credential_present: The snapshot presence boolean only.
            expected_config_source: The exact active-YAML path string bound into
                the child environment; compared to the reported source verbatim.
        """
        self._client = client
        self._inputs = inputs
        self._run_id = run_id
        self._started_at_utc = started_at_utc
        self._device_configs = device_configs
        self._profiles = profiles
        self._root = root
        self._descriptor = descriptor
        self._credential_env_var_name = credential_env_var_name
        self._credential_present = credential_present
        self._expected_config_source = expected_config_source

    async def freeze(self, phase: ColdPhaseKind) -> ColdRunIdentity:
        """Read server info then runtime config, cross-check, and freeze the phase identity.

        On every phase, before delegating to ``freeze_identity``, the reported
        server version and the declared host version must both equal the public
        pin exactly, and the reported config source must equal the bound active
        path exactly (no normalisation or filesystem use).

        Args:
            phase: The phase being frozen.

        Returns:
            The frozen identity.

        Raises:
            ColdCompositionChildError: If a version or the config source does not
                match exactly (fixed message, no response content).  Read and
                freeze exceptions propagate unchanged.
        """
        server = await self._client.get_server_info()
        runtime = await self._client.get_runtime_config()
        inputs, host = self._inputs, self._inputs.host_facts
        if not (
            server.version == REQUIRED_MCP_VERSION
            and host.coffee_roaster_mcp_version == REQUIRED_MCP_VERSION
        ):
            raise ColdCompositionChildError("Cold identity refused.")
        if runtime.config_source != self._expected_config_source:
            raise ColdCompositionChildError("Cold identity refused.")
        return freeze_identity(
            run_id=self._run_id,
            started_at_utc=self._started_at_utc,
            coffee_roaster_mcp_version=host.coffee_roaster_mcp_version,
            python_version=host.python_version,
            platform=host.platform,
            machine=host.machine,
            operating_system=host.operating_system,
            kernel=host.kernel,
            pi_model=host.pi_model,
            pi_revision=host.pi_revision,
            runtime_config=runtime,
            server_info=server,
            device_config=self._device_configs[phase],
            build_provenance=inputs.build_provenance,
            effective_mcp_profile=self._profiles[phase],
            audio_device_identity=inputs.audio_device_identity,
            serial_port_path=inputs.serial_port_path,
            controller_tick_seconds=COLD_OBSERVATION_INTERVAL_SECONDS,
            pi_evidence_root=self._root.path,
            laptop_evidence_root=inputs.laptop_evidence_root,
            advisor_descriptor=self._descriptor,
            credential_env_var_name=self._credential_env_var_name,
            credential_present=self._credential_present,
            stimulus_block=inputs.stimulus_block,
            operator_host_notes=inputs.operator_host_notes,
            operator_psu_notes=inputs.operator_psu_notes,
            operator_cooling_notes=inputs.operator_cooling_notes,
            boot_id_path=host.boot_id_path,
        )


def _construct_process(
    config: MCPConfig, environment: Mapping[str, str], family: str
) -> ColdMCPServerProcess | ColdCompositionRefusal:
    """Step 9: construct the single cold process; a constructor refusal is closed."""
    try:
        return ColdMCPServerProcess(config, child_environment=environment, family=family)
    except ValueError:
        return ColdCompositionRefusal.MCP_ENV_NOT_ADMITTED


class _SingleUseAdvisorFactory:
    """Returns the admitted advisor exactly once."""

    def __init__(self, advisor: ColdAdvisoryAdvisorPort) -> None:
        self._advisor: ColdAdvisoryAdvisorPort | None = advisor

    def __call__(self) -> ColdAdvisoryAdvisorPort:
        advisor, self._advisor = self._advisor, None
        if advisor is None:
            raise ColdCompositionChildError("Cold advisor factory refused.")
        return advisor


async def run_cold_characterisation(
    config: AppConfig,
    inputs: ColdCompositionInputs,
    *,
    resources: ColdCompositionResources,
    clock: ColdEngineClock,
    host: ColdEngineHost,
    advisor_builder: Callable[[AppConfig], RoastAdvisor | None] = build_advisor,
    run_suffix: Callable[[], str] = random_run_suffix,
) -> ColdTwoPhaseResult | ColdCompositionRefusal:
    """Admit everything, construct one child and client, and run both cold phases.

    Every :class:`ColdCompositionRefusal` check runs before the child process
    object is constructed; such a refusal returns its closed member with nothing
    constructed.  The per-phase identity guards (MCP version and config source)
    run later, after the child starts, and surface through the runtime as an
    identity-not-frozen refusal; the runtime then attempts to stop the child and
    reports the resulting ownership status, which may be unconfirmed (a port can
    also stall), so no physically safe state is inferred from it.  The runtime's
    result, including a pending provider check, is returned as-is.

    Caller obligation: do not start another cold run while a preceding child's
    shutdown remains unconfirmed, including after a propagated exception or a
    not-owned result (neither proves the child is absent).  This function keeps
    no process-wide retry latch and grants no termination authority; uncertain
    child ownership alone authorises nothing.  Same-user tampering, the SDK
    merge-order dependency, the independent operator emergency stop and the
    hardware residuals remain.

    Args:
        config: The application config (advisor, safety, controller timing, MCP).
        inputs: The explicit caller inputs.
        resources: Entered private resources; the caller owns cleanup.
        clock: The engine clock (one instance for both phases).
        host: The host-bound port (one instance for both phases).
        advisor_builder: Builds the advisor once; production is the real builder.
        run_suffix: Supplies the run-ID suffix.

    Returns:
        The runtime's closed result, or the first refusal.
    """
    directory = resources.directory
    if directory is None:
        return ColdCompositionRefusal.RESOURCES_NOT_ADMITTED
    family = os.name
    snapshot = _admit_environment(config, family)
    if isinstance(snapshot, ColdCompositionRefusal):
        return snapshot
    if snapshot.credential_present is not True:
        return ColdCompositionRefusal.CREDENTIAL_ABSENT
    admitted = _admit_advisor(advisor_builder, config)
    if isinstance(admitted, ColdCompositionRefusal):
        return admitted
    advisor, descriptor = admitted
    device_configs = _admit_device_config(inputs.device_config)
    if isinstance(device_configs, ColdCompositionRefusal):
        return device_configs
    minted = _mint_run_id(clock, run_suffix)
    if isinstance(minted, ColdCompositionRefusal):
        return minted
    run_id, started_at_utc = minted
    root = _admit_root(inputs)
    if isinstance(root, ColdCompositionRefusal):
        return root
    rendered = _snapshot_and_render(directory, device_configs)
    if isinstance(rendered, ColdCompositionRefusal):
        return rendered
    profiles = _admit_profiles(rendered)
    if isinstance(profiles, ColdCompositionRefusal):
        return profiles
    active_path = directory / _ACTIVE_NAME
    process = _construct_process(
        config.mcp,
        _cold_child_environment(snapshot.values, family, active_path),
        family,
    )
    if isinstance(process, ColdCompositionRefusal):
        return process
    client = ColdCharacterisationMCPClient(process.call_tool)
    child: ColdChildLifecycle = ColdMCPChild(
        process,
        directory=directory,
        rendered={phase: item.data for phase, item in rendered.items()},
    )
    identities: ColdPhaseIdentitySource = ColdIdentitySource(
        client=client,
        inputs=inputs,
        run_id=run_id,
        started_at_utc=started_at_utc,
        device_configs=device_configs,
        profiles=profiles,
        root=root,
        descriptor=descriptor,
        credential_env_var_name=config.advisor.api_key_env,
        credential_present=snapshot.credential_present,
        expected_config_source=str(active_path),
    )
    return await run_two_phase_characterisation(
        root=root,
        mcp=client,
        child=child,
        identities=identities,
        host=host,
        clock=clock,
        advisor_factory=_SingleUseAdvisorFactory(advisor),
        spec=inputs.spec,
        configured_call_bound_seconds=float(config.controller.advisory_timeout_seconds),
        configured_dwell_seconds=float(config.controller.post_fc_min_consult_interval_seconds),
        evaluator=SafetyPolicy(config.safety),
    )
