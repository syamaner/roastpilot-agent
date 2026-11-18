"""Pure launch composition for the coffee-roaster-mcp child (#1078 C1).

This domain module owns the closed authority grammar, child-config selection
mirror, identity validation of an already-parsed configuration document, the
requested-capture value type and its producer-derived topology, deterministic
child environment composition, HUMAN pin rendering, the consumer-owned launch
proof policy, and the evidence/record types.

It performs no I/O: it never opens a file, logs, reads the process environment
or touches the MCP SDK.  The infrastructure transport
(:mod:`roastpilot_agent.mcp_client`) reads the selected configuration file and
binds a record to the child epoch; every refusal decision is made here.

Evidence and records describe *requested/configured* composition consumed at
one attach epoch.  They are not effective child configuration, measured audio,
physical capture or artifact identity, and they do not prove the child later
read the same configuration bytes.  Admission for human use stays closed until
a later strict post-attach readback (C2) proves those facts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import cast

from roastpilot_agent.human_protection import ControlAuthority

#: The producer's only first-crack authority environment carrier.
AUTHORITY_ENV_KEY = "COFFEE_FIRST_CRACK_EVENT_AUTHORITY"
#: Inherited keys that can carry human authority into a child.
HUMAN_AUTHORITY_ENV_KEYS = frozenset({AUTHORITY_ENV_KEY})
#: The producer's configuration-path environment key.
CONFIG_PATH_ENV_KEY = "COFFEE_ROASTER_MCP_CONFIG"
#: The producer's working-directory default configuration file name.
DEFAULT_CHILD_CONFIG_FILENAME = "coffee-roaster-mcp.yaml"
#: The producer's manual evidence publisher stops above this many streams.
MAX_CAPTURE_STREAMS = 8

_DRIVER_ENV_KEY = "COFFEE_ROASTER_DRIVER"
_MODE_ENV_KEY = "COFFEE_FIRST_CRACK_MODE"
_AUDIO_SOURCE_ENV_KEY = "COFFEE_AUDIO_SOURCE"
_AUDIO_INPUT_DEVICE_ENV_KEY = "COFFEE_AUDIO_INPUT_DEVICE"
_AUDIO_WAV_PATH_ENV_KEY = "COFFEE_AUDIO_WAV_PATH"
_AUDIO_REPLAY_MODE_ENV_KEY = "COFFEE_AUDIO_REPLAY_MODE"
_AUDIO_SAMPLE_RATE_ENV_KEY = "COFFEE_AUDIO_SAMPLE_RATE"
_AUDIO_WINDOW_ENV_KEY = "COFFEE_AUDIO_WINDOW_SECONDS"
_AUDIO_OVERLAP_ENV_KEY = "COFFEE_AUDIO_OVERLAP"
_AUDIO_HOP_ENV_KEY = "COFFEE_AUDIO_HOP_SECONDS"
_RECORDING_ENABLED_ENV_KEY = "COFFEE_RECORDING_ENABLED"
_RECORDING_AUTOCAPTURE_ENV_KEY = "COFFEE_RECORDING_AUTOCAPTURE"
_RECORDING_SAMPLE_RATE_ENV_KEY = "COFFEE_RECORDING_SAMPLE_RATE"
_RECORDING_DEVICES_ENV_KEY = "COFFEE_RECORDING_DEVICES"

#: Exactly the 15 environment keys pinned for a HUMAN (manual/mock) launch.
HUMAN_PIN_KEYS = frozenset(
    {
        AUTHORITY_ENV_KEY,
        _DRIVER_ENV_KEY,
        _MODE_ENV_KEY,
        _AUDIO_SOURCE_ENV_KEY,
        _AUDIO_INPUT_DEVICE_ENV_KEY,
        _AUDIO_WAV_PATH_ENV_KEY,
        _AUDIO_REPLAY_MODE_ENV_KEY,
        _AUDIO_SAMPLE_RATE_ENV_KEY,
        _AUDIO_WINDOW_ENV_KEY,
        _AUDIO_OVERLAP_ENV_KEY,
        _AUDIO_HOP_ENV_KEY,
        _RECORDING_ENABLED_ENV_KEY,
        _RECORDING_AUTOCAPTURE_ENV_KEY,
        _RECORDING_SAMPLE_RATE_ENV_KEY,
        _RECORDING_DEVICES_ENV_KEY,
    }
)

_FIRST_CRACK_SECTION = "first_crack"
_ROASTER_SECTION = "roaster"
_AUTHORITY_YAML_KEY = "event_authority"
_DRIVER_YAML_KEY = "driver"
_MODE_YAML_KEY = "mode"
_MODES = frozenset({"disabled", "audio", "manual"})


class AuthorityClass(Enum):
    """Classification of one authority carrier value."""

    ABSENT = "absent"
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    INVALID = "invalid"


class LaunchProofStatus(Enum):
    """Whether the spawned parameters are the base-composed, unchanged parameters."""

    ABSENT = "absent"
    MATCHED = "matched"
    MISMATCHED = "mismatched"


class LaunchRefusal(Enum):
    """Closed reasons a launch is refused before the child is spawned."""

    EXPLICIT_AUTHORITY_INCOMPATIBLE = "explicit_authority_incompatible"
    INHERITED_AUTHORITY_INCOMPATIBLE = "inherited_authority_incompatible"
    EXPLICIT_PIN_CONFLICT = "explicit_pin_conflict"
    CONFIG_AUTHORITY_INCOMPATIBLE = "config_authority_incompatible"
    CONFIG_IDENTITY_CONFLICT = "config_identity_conflict"
    CONFIG_SOURCE_UNREADABLE = "config_source_unreadable"
    CONFIG_SOURCE_INVALID = "config_source_invalid"
    PIN_NOT_CONSUMED = "pin_not_consumed"
    LAUNCH_COMPOSITION_UNPROVEN = "launch_composition_unproven"


class AudioSourceKind(Enum):
    """Requested first-crack detector audio source."""

    MICROPHONE = "microphone"
    WAV = "wav"


class HopSource(Enum):
    """Whether the detector hop was requested explicitly or derived."""

    EXPLICIT = "explicit"
    DERIVED_FROM_OVERLAP = "derived_from_overlap"


class RecorderStreamSource(Enum):
    """How the producer feeds one recorder stream."""

    DETECTOR_TEE = "detector_tee"
    OPENED_SELECTOR = "opened_selector"


class ChildEnvBase(Enum):
    """Which environment base the SDK applies to the child."""

    SDK_DEFAULT = "sdk_default"
    AGENT_ENVIRONMENT_MERGE = "agent_environment_merge"


class ConfigSelectionKind(Enum):
    """Which configuration source the child's loader will select."""

    ENV_PATH = "env_path"
    CWD_DEFAULT = "cwd_default"


class ConfigReadStatus(Enum):
    """Outcome of the infrastructure read of the selected configuration."""

    MISSING = "missing"
    LOADED = "loaded"
    UNREADABLE = "unreadable"


class LaunchRefusalError(Exception):
    """A launch refusal; the message names the refusal and at most a key, never a value."""

    def __init__(self, refusal: LaunchRefusal, key: str | None = None) -> None:
        """Build the refusal.

        Args:
            refusal: The closed refusal reason.
            key: The environment or configuration key name involved, if any.
        """
        super().__init__(refusal.name if key is None else f"{refusal.name}: {key}")
        self.refusal = refusal
        self.key = key


def require_launch_proof(authority: ControlAuthority, status: LaunchProofStatus) -> None:
    """Apply the consumer-owned launch proof policy.

    ``MATCHED`` is admitted in either authority, ``MISMATCHED`` is refused in
    either authority, and ``ABSENT`` (a legacy builder that never used base
    composition) is admitted only for the preserved AUTOMATIC path.

    Args:
        authority: The authority this transport launches for.
        status: The infrastructure's identity/all-field proof comparison.

    Raises:
        TypeError: If either argument is not exactly the expected enum type.
        LaunchRefusalError: ``LAUNCH_COMPOSITION_UNPROVEN`` on a refused cell.
    """
    if type(authority) is not ControlAuthority:
        raise TypeError("authority must be a ControlAuthority")
    if type(status) is not LaunchProofStatus:
        raise TypeError("status must be a LaunchProofStatus")
    if status is LaunchProofStatus.MATCHED:
        return
    if status is LaunchProofStatus.ABSENT and authority is ControlAuthority.AUTOMATIC:
        return
    raise LaunchRefusalError(LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN)


def classify_authority(value: object) -> AuthorityClass:
    """Classify a present authority value with the producer's grammar.

    Args:
        value: The carrier value (environment string or parsed YAML value).

    Returns:
        ``AUTOMATIC`` or ``MANUAL`` for a string matching after strip/lower,
        else ``INVALID``.  ``ABSENT`` is produced only by callers.
    """
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised == "automatic":
            return AuthorityClass.AUTOMATIC
        if normalised == "manual":
            return AuthorityClass.MANUAL
    return AuthorityClass.INVALID


@dataclass(frozen=True, slots=True)
class ChildConfigSelection:
    """The configuration source the child's loader selects.

    ``path_text`` is the stripped environment path, or the default file name
    resolved against the inherited working directory.
    """

    kind: ConfigSelectionKind
    path_text: str


def select_child_config(env: Mapping[str, str] | None) -> ChildConfigSelection:
    """Mirror the producer's configuration-path selection (``path=None``).

    Args:
        env: The child's composed environment, or ``None`` for the SDK default
            (which never contains the configuration-path key).

    Returns:
        The environment path when non-empty after strip, else the cwd default.
    """
    if env is not None:
        value = env.get(CONFIG_PATH_ENV_KEY)
        if isinstance(value, str) and value.strip():
            return ChildConfigSelection(ConfigSelectionKind.ENV_PATH, value.strip())
    return ChildConfigSelection(ConfigSelectionKind.CWD_DEFAULT, DEFAULT_CHILD_CONFIG_FILENAME)


@dataclass(frozen=True, slots=True)
class ChildConfigRead:
    """The infrastructure read of the selected configuration.

    ``document`` is the ``yaml.safe_load`` result and is meaningful only when
    ``status`` is ``LOADED``.
    """

    status: ConfigReadStatus
    document: object = None

    def __post_init__(self) -> None:
        """Require an exact read status."""
        if type(self.status) is not ConfigReadStatus:
            raise TypeError("status must be a ConfigReadStatus")


@dataclass(frozen=True, slots=True)
class ChildConfigIdentity:
    """Authority, driver and mode identity of the selected configuration."""

    present: bool
    authority: AuthorityClass
    driver: str | None
    mode: str | None


def _section(document: Mapping[object, object], name: str) -> Mapping[object, object]:
    value = document.get(name, {})
    if not isinstance(value, Mapping):
        raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID, name)
    return cast("Mapping[object, object]", value)


def child_config_identity(
    read: ChildConfigRead, selection: ChildConfigSelection
) -> ChildConfigIdentity:
    """Validate the structure of a parsed configuration and extract its identity.

    Every structural or type refusal here is one the bound producer loader
    also raises on the same bytes.

    Args:
        read: The infrastructure read result.
        selection: The selection the read used.

    Returns:
        The identity; all fields absent when a cwd default is missing.

    Raises:
        TypeError: If ``read`` is not exactly a :class:`ChildConfigRead`.
        LaunchRefusalError: ``CONFIG_SOURCE_UNREADABLE`` or ``CONFIG_SOURCE_INVALID``.
    """
    if type(read) is not ChildConfigRead:
        raise TypeError("read must be a ChildConfigRead")
    if type(read.status) is not ConfigReadStatus:
        raise TypeError("status must be a ConfigReadStatus")
    if read.status is ConfigReadStatus.UNREADABLE:
        raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_UNREADABLE, CONFIG_PATH_ENV_KEY)
    if read.status is ConfigReadStatus.MISSING:
        if selection.kind is ConfigSelectionKind.ENV_PATH:
            raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_UNREADABLE, CONFIG_PATH_ENV_KEY)
        return ChildConfigIdentity(
            present=False, authority=AuthorityClass.ABSENT, driver=None, mode=None
        )
    raw: object = {} if read.document is None else read.document
    if not isinstance(raw, Mapping):
        raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID)
    document = cast("Mapping[object, object]", raw)
    first_crack = _section(document, _FIRST_CRACK_SECTION)
    roaster = _section(document, _ROASTER_SECTION)

    authority = AuthorityClass.ABSENT
    if _AUTHORITY_YAML_KEY in first_crack:
        authority = classify_authority(first_crack[_AUTHORITY_YAML_KEY])
        if authority is AuthorityClass.INVALID:
            raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID, _AUTHORITY_YAML_KEY)

    driver: str | None = None
    if _DRIVER_YAML_KEY in roaster:
        raw_driver = roaster[_DRIVER_YAML_KEY]
        if not isinstance(raw_driver, str):
            raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID, _DRIVER_YAML_KEY)
        driver = raw_driver

    mode: str | None = None
    if _MODE_YAML_KEY in first_crack:
        raw_mode = first_crack[_MODE_YAML_KEY]
        if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in _MODES:
            raise LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID, _MODE_YAML_KEY)
        mode = raw_mode.strip().lower()

    return ChildConfigIdentity(present=True, authority=authority, driver=driver, mode=mode)


def _require_rate(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_text(value: object, name: str, *, selector: bool) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a str")
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty, stripped and NUL-free")
    if selector and "," in value:
        raise ValueError(f"{name} must not contain a comma")


@dataclass(frozen=True, slots=True)
class DetectorTopology:
    """The requested detector stream (requested values, not measured capture)."""

    source: AudioSourceKind
    selector: str | None
    wav_path: str | None
    sample_rate_hz: int
    window_seconds: float
    overlap: float
    hop_source: HopSource
    explicit_hop_seconds: float | None
    effective_hop_seconds: float


@dataclass(frozen=True, slots=True)
class RecorderStream:
    """One requested recorder stream with the producer's label derivation."""

    label: str | None
    source: RecorderStreamSource
    sample_rate_hz: int


@dataclass(frozen=True, slots=True)
class CaptureTopology:
    """Producer-derived detector and recorder topology of a requested capture."""

    detector: DetectorTopology
    recorder_primary: RecorderStream
    recorder_additional: tuple[RecorderStream, ...]


@dataclass(frozen=True, slots=True)
class RequestedCapture:
    """A requested manual/mock first-crack capture configuration.

    Selectors are the producer's *substring* device selectors, not device
    identities.  Every value is requested configuration; nothing here is
    measured capture or proof of a physical device.
    """

    source: AudioSourceKind
    detector_selector: str | None
    wav_path: str | None
    detector_sample_rate_hz: int
    window_seconds: float
    overlap: float
    hop_seconds: float | None
    additional_selectors: tuple[str, ...] = ()
    additional_sample_rate_hz: int | None = None

    def __post_init__(self) -> None:
        """Validate every closed clause.

        Raises:
            TypeError: On a wrong value type.
            ValueError: On a refused value or combination.
        """
        if type(self.source) is not AudioSourceKind:
            raise TypeError("source must be an AudioSourceKind")
        _require_rate(self.detector_sample_rate_hz, "detector_sample_rate_hz")
        if type(self.window_seconds) is not float:
            raise TypeError("window_seconds must be a float")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0.0:
            raise ValueError("window_seconds must be finite and positive")
        if type(self.overlap) is not float:
            raise TypeError("overlap must be a float")
        if not math.isfinite(self.overlap) or not 0.0 <= self.overlap < 1.0:
            raise ValueError("overlap must be finite in [0, 1)")
        if self.hop_seconds is not None:
            if type(self.hop_seconds) is not float:
                raise TypeError("hop_seconds must be a float or None")
            if not math.isfinite(self.hop_seconds) or not (
                0.0 < self.hop_seconds <= self.window_seconds
            ):
                raise ValueError("hop_seconds must be finite in (0, window_seconds]")
        effective = self._effective_hop()
        if not math.isfinite(effective) or effective <= 0.0:
            raise ValueError("effective hop must be finite and positive")
        if self.detector_selector is not None:
            _require_text(self.detector_selector, "detector_selector", selector=True)
        if self.wav_path is not None:
            _require_text(self.wav_path, "wav_path", selector=False)
        if type(self.additional_selectors) is not tuple:
            raise TypeError("additional_selectors must be a tuple")
        for selector in self.additional_selectors:
            _require_text(selector, "additional_selectors", selector=True)
        if self.source is AudioSourceKind.WAV:
            if self.wav_path is None:
                raise ValueError("WAV source requires wav_path")
            if self.detector_selector is not None:
                raise ValueError("WAV source takes no detector_selector")
            if self.additional_selectors:
                raise ValueError("WAV source takes no additional selectors")
        elif self.wav_path is not None:
            raise ValueError("microphone source takes no wav_path")
        if self.additional_selectors:
            if self.detector_selector is None:
                raise ValueError("additional selectors require a named detector selector")
            if len(set(self.additional_selectors)) != len(self.additional_selectors):
                raise ValueError("additional selectors must be unique")
            if self.detector_selector in self.additional_selectors:
                raise ValueError("additional selectors must differ from the detector selector")
            if 1 + len(self.additional_selectors) > MAX_CAPTURE_STREAMS:
                raise ValueError("too many capture streams")
            if self.additional_sample_rate_hz is not None:
                _require_rate(self.additional_sample_rate_hz, "additional_sample_rate_hz")
        elif self.additional_sample_rate_hz is not None:
            raise ValueError("additional_sample_rate_hz requires additional selectors")

    def _effective_hop(self) -> float:
        if self.hop_seconds is not None:
            return self.hop_seconds
        return self.window_seconds * (1.0 - self.overlap)

    def topology(self) -> CaptureTopology:
        """Derive the producer's detector/recorder wiring for this request.

        Returns:
            The requested topology; labels follow the producer's recorder
            construction (single named → selector; default or WAV → ``None``;
            paired → selector then the additional selectors in order).
        """
        detector = DetectorTopology(
            source=self.source,
            selector=self.detector_selector,
            wav_path=self.wav_path,
            sample_rate_hz=self.detector_sample_rate_hz,
            window_seconds=self.window_seconds,
            overlap=self.overlap,
            hop_source=(
                HopSource.EXPLICIT
                if self.hop_seconds is not None
                else HopSource.DERIVED_FROM_OVERLAP
            ),
            explicit_hop_seconds=self.hop_seconds,
            effective_hop_seconds=self._effective_hop(),
        )
        primary = RecorderStream(
            label=self.detector_selector,
            source=RecorderStreamSource.DETECTOR_TEE,
            sample_rate_hz=self.detector_sample_rate_hz,
        )
        additional_rate = self.additional_sample_rate_hz or self.detector_sample_rate_hz
        additional = tuple(
            RecorderStream(
                label=selector,
                source=RecorderStreamSource.OPENED_SELECTOR,
                sample_rate_hz=additional_rate,
            )
            for selector in self.additional_selectors
        )
        return CaptureTopology(
            detector=detector, recorder_primary=primary, recorder_additional=additional
        )


def _recording_devices(capture: RequestedCapture) -> str:
    if capture.detector_selector is None:
        return ""
    return ",".join((capture.detector_selector, *capture.additional_selectors))


def render_human_pins(capture: RequestedCapture) -> tuple[tuple[str, str], ...]:
    """Render the 15 HUMAN environment pins for a requested capture.

    Args:
        capture: The exact requested capture.

    Returns:
        ``(key, value)`` pairs sorted by key.

    Raises:
        TypeError: If ``capture`` is not exactly a :class:`RequestedCapture`.
    """
    if type(capture) is not RequestedCapture:
        raise TypeError("capture must be a RequestedCapture")
    pins = {
        AUTHORITY_ENV_KEY: "manual",
        _DRIVER_ENV_KEY: "mock",
        _MODE_ENV_KEY: "audio",
        _AUDIO_SOURCE_ENV_KEY: capture.source.value,
        _AUDIO_INPUT_DEVICE_ENV_KEY: capture.detector_selector or "",
        _AUDIO_WAV_PATH_ENV_KEY: capture.wav_path or "",
        _AUDIO_REPLAY_MODE_ENV_KEY: "realtime",
        _AUDIO_SAMPLE_RATE_ENV_KEY: str(capture.detector_sample_rate_hz),
        _AUDIO_WINDOW_ENV_KEY: repr(capture.window_seconds),
        _AUDIO_OVERLAP_ENV_KEY: repr(capture.overlap),
        _AUDIO_HOP_ENV_KEY: "" if capture.hop_seconds is None else repr(capture.hop_seconds),
        _RECORDING_ENABLED_ENV_KEY: "true",
        _RECORDING_AUTOCAPTURE_ENV_KEY: "true",
        _RECORDING_SAMPLE_RATE_ENV_KEY: (
            ""
            if capture.additional_sample_rate_hz is None
            else str(capture.additional_sample_rate_hz)
        ),
        _RECORDING_DEVICES_ENV_KEY: _recording_devices(capture),
    }
    return tuple(sorted(pins.items()))


def _merge_child_env(
    *,
    base: Mapping[str, str],
    explicit: Mapping[str, str],
    pins: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Merge base, then explicit, then pins (later wins)."""
    merged = dict(base)
    merged.update(explicit)
    merged.update(pins)
    return merged


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    """Composition step 1: the child env and the selection the child will use.

    ``env`` is transient transport data and is never part of the evidence.
    """

    env: dict[str, str] | None
    env_base: ChildEnvBase
    selection: ChildConfigSelection
    authority: ControlAuthority
    override_authority: AuthorityClass
    inherited_authority: AuthorityClass
    pinned_env: tuple[tuple[str, str], ...]
    capture: RequestedCapture | None
    topology: CaptureTopology | None


def _carrier_class(env: Mapping[str, str]) -> AuthorityClass:
    if AUTHORITY_ENV_KEY not in env:
        return AuthorityClass.ABSENT
    return classify_authority(env[AUTHORITY_ENV_KEY])


def prepare_child_launch(
    *,
    explicit_env: Mapping[str, str],
    inherited_env: Mapping[str, str],
    capture: RequestedCapture | None,
) -> PreparedLaunch:
    """Compose the child environment and validate authority carriers.

    ``capture is None`` is AUTOMATIC: an explicit non-automatic or inherited
    non-automatic authority is refused, the inherited authority is withheld
    from the merge base, and an empty explicit map keeps the SDK default
    (``env=None``).  A requested capture is HUMAN: the 15 pins are applied
    last over base and explicit values, an explicit differing pin or an
    inherited non-manual authority is refused, and every pin must survive.
    Inputs are never mutated.

    Args:
        explicit_env: The override map presented to the builder.
        inherited_env: The Agent process environment.
        capture: The requested HUMAN capture, or ``None`` for AUTOMATIC.

    Returns:
        The prepared launch.

    Raises:
        TypeError: If ``capture`` is neither ``None`` nor exactly a RequestedCapture.
        LaunchRefusalError: On an incompatible carrier or an unconsumed pin.
    """
    if capture is not None and type(capture) is not RequestedCapture:
        raise TypeError("capture must be a RequestedCapture or None")
    explicit = dict(explicit_env)
    inherited = dict(inherited_env)
    override_class = _carrier_class(explicit)
    inherited_class = _carrier_class(inherited)
    base = {k: v for k, v in inherited.items() if k not in HUMAN_AUTHORITY_ENV_KEYS}

    if capture is None:
        if override_class not in (AuthorityClass.ABSENT, AuthorityClass.AUTOMATIC):
            raise LaunchRefusalError(
                LaunchRefusal.EXPLICIT_AUTHORITY_INCOMPATIBLE, AUTHORITY_ENV_KEY
            )
        if inherited_class not in (AuthorityClass.ABSENT, AuthorityClass.AUTOMATIC):
            raise LaunchRefusalError(
                LaunchRefusal.INHERITED_AUTHORITY_INCOMPATIBLE, AUTHORITY_ENV_KEY
            )
        if not explicit:
            env: dict[str, str] | None = None
            env_base = ChildEnvBase.SDK_DEFAULT
        else:
            env = _merge_child_env(base=base, explicit=explicit, pins=())
            env_base = ChildEnvBase.AGENT_ENVIRONMENT_MERGE
        return PreparedLaunch(
            env=env,
            env_base=env_base,
            selection=select_child_config(env),
            authority=ControlAuthority.AUTOMATIC,
            override_authority=override_class,
            inherited_authority=inherited_class,
            pinned_env=(),
            capture=None,
            topology=None,
        )

    pins = render_human_pins(capture)
    for key, value in pins:
        if key in explicit and explicit[key] != value:
            raise LaunchRefusalError(LaunchRefusal.EXPLICIT_PIN_CONFLICT, key)
    if inherited_class not in (AuthorityClass.ABSENT, AuthorityClass.MANUAL):
        raise LaunchRefusalError(LaunchRefusal.INHERITED_AUTHORITY_INCOMPATIBLE, AUTHORITY_ENV_KEY)
    human_env = _merge_child_env(base=base, explicit=explicit, pins=pins)
    for key, value in pins:
        if human_env.get(key) != value:
            raise LaunchRefusalError(LaunchRefusal.PIN_NOT_CONSUMED, key)
    return PreparedLaunch(
        env=human_env,
        env_base=ChildEnvBase.AGENT_ENVIRONMENT_MERGE,
        selection=select_child_config(human_env),
        authority=ControlAuthority.HUMAN,
        override_authority=override_class,
        inherited_authority=inherited_class,
        pinned_env=pins,
        capture=capture,
        topology=capture.topology(),
    )


@dataclass(frozen=True, slots=True)
class LaunchEvidence:
    """Requested/configured composition consumed at one attach epoch.

    This is not effective child configuration, measured audio, physical
    capture or artifact identity, and it does not prove the child read the
    same configuration bytes later.  It holds no full environment, config
    path, command, version or digest.  ``override_authority`` classifies the
    override map presented to the builder (which may include an agreeing
    forwarded shell value); ``inherited_authority`` classifies the
    still-present process carrier.  The requested capture intentionally
    carries selectors, an optional WAV path and the pin values as requested
    configuration only.
    """

    authority: ControlAuthority
    env_base: ChildEnvBase
    config_selection: ConfigSelectionKind
    config_present: bool
    config_authority: AuthorityClass
    override_authority: AuthorityClass
    inherited_authority: AuthorityClass
    pinned_env: tuple[tuple[str, str], ...]
    capture: RequestedCapture | None
    topology: CaptureTopology | None


@dataclass(frozen=True, slots=True)
class LaunchComposition:
    """Composition step 2 result: the transient child env and its evidence."""

    env: dict[str, str] | None
    evidence: LaunchEvidence


def finalize_child_launch(prepared: PreparedLaunch, read: ChildConfigRead) -> LaunchComposition:
    """Validate the selected configuration against the prepared authority.

    AUTOMATIC refuses a configuration whose authority is manual, regardless of
    agreeing explicit/inherited carriers.  HUMAN refuses a configuration whose
    present authority is not manual, driver is not exactly ``mock`` or mode is
    not ``audio``; capture values in the configuration are overridden by pins.

    Args:
        prepared: Step 1 output.
        read: The infrastructure read of ``prepared.selection``.

    Returns:
        The composition with its evidence.

    Raises:
        TypeError: If ``prepared`` or ``read`` has the wrong type.
        LaunchRefusalError: On a config source, structure or identity refusal.
    """
    if type(prepared) is not PreparedLaunch:
        raise TypeError("prepared must be a PreparedLaunch")
    identity = child_config_identity(read, prepared.selection)
    if prepared.authority is ControlAuthority.AUTOMATIC:
        if identity.authority is AuthorityClass.MANUAL:
            raise LaunchRefusalError(
                LaunchRefusal.CONFIG_AUTHORITY_INCOMPATIBLE, _AUTHORITY_YAML_KEY
            )
    else:
        if identity.authority not in (AuthorityClass.ABSENT, AuthorityClass.MANUAL):
            raise LaunchRefusalError(LaunchRefusal.CONFIG_IDENTITY_CONFLICT, _AUTHORITY_YAML_KEY)
        if identity.driver is not None and identity.driver != "mock":
            raise LaunchRefusalError(LaunchRefusal.CONFIG_IDENTITY_CONFLICT, _DRIVER_YAML_KEY)
        if identity.mode is not None and identity.mode != "audio":
            raise LaunchRefusalError(LaunchRefusal.CONFIG_IDENTITY_CONFLICT, _MODE_YAML_KEY)
    evidence = LaunchEvidence(
        authority=prepared.authority,
        env_base=prepared.env_base,
        config_selection=prepared.selection.kind,
        config_present=identity.present,
        config_authority=identity.authority,
        override_authority=prepared.override_authority,
        inherited_authority=prepared.inherited_authority,
        pinned_env=prepared.pinned_env,
        capture=prepared.capture,
        topology=prepared.topology,
    )
    env = None if prepared.env is None else dict(prepared.env)
    return LaunchComposition(env=env, evidence=evidence)


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """Requested/configured composition bound to the epoch that consumed it.

    The record answers only for ``child_epoch``.  It is not effective
    configuration, measured audio, physical capture or artifact identity, and
    does not prove the child read the same configuration bytes later; C2's
    strict post-attach readback must, and admission stays closed until then.
    """

    child_epoch: int
    evidence: LaunchEvidence


def bind_launch_record(evidence: LaunchEvidence, child_epoch: int) -> LaunchRecord:
    """Bind evidence to the positive child epoch that consumed it.

    Args:
        evidence: The evidence of the consumed parameters.
        child_epoch: The attached child's epoch.

    Returns:
        The record.

    Raises:
        TypeError: If the types are not exact.
        ValueError: If ``child_epoch`` is not positive.
    """
    if type(evidence) is not LaunchEvidence:
        raise TypeError("evidence must be a LaunchEvidence")
    if type(child_epoch) is not int:
        raise TypeError("child_epoch must be an int")
    if child_epoch <= 0:
        raise ValueError("child_epoch must be positive")
    return LaunchRecord(child_epoch=child_epoch, evidence=evidence)
