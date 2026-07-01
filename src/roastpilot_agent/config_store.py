"""Saved-config file persistence and per-field metadata for the Config UI (D76/D78, #418).

The agent owns one unified config. Today :class:`~roastpilot_agent.config.AppConfig`
reads only ``ROASTPILOT_*`` environment variables. This module adds a writable
**saved-config file** as the operator surface, layered UNDER the environment so
``roast-live.sh`` and CI's ``FakeAdvisor`` keep working untouched.

Resolution precedence (env-overrides-file, D78 constraint 1)::

    effective = env_override ?? saved_file ?? default

``GET /api/config`` exposes, per managed field:

- ``saved_value`` — what the operator last wrote to the file (``None`` if absent
  from the file, i.e. still at default).
- ``effective_value`` — the value the running agent actually uses (env wins over
  saved over default).
- ``default`` — the schema default for this field.
- ``env_overridden`` — ``True`` when a ``ROASTPILOT_*`` env var shadows the
  saved value; the FE renders an "overridden by env" badge for these.
- ``read_only`` — ``True`` for fields that must not appear in a ``PUT`` body
  (all :class:`~roastpilot_agent.config.SafetyLimits` in M1, controller tick,
  FC model, API-key env name).

The saved file is YAML. Only the *managed editable* fields are ever written — the
full :class:`~roastpilot_agent.config.AppConfig` serialisation lives in the env /
defaults path and is never redundantly mirrored here.

**Device-enum spike decision (D78, for PR (c)):**

  Agent-direct ``pyserial.tools.list_ports`` for serial (in-process, no MCP
  running between roasts) + an MCP ``list-devices`` capability for audio (avoids
  adding PortAudio/sounddevice to the agent's runtime deps). Rationale: serial
  enumeration is pure OS I/O, available whenever the agent runs, and
  ``pyserial`` is already an indirect dep of the MCP; audio device listing
  requires PortAudio linkage, which is a heavy optional dep and is most
  accurately reported by the MCP process that owns the audio hardware anyway.
  This decision is noted here; the endpoint is implemented in PR (c).

**PR (b) — file-lock around persist_config_edit (DONE):** :func:`persist_config_edit`
now holds a ``filelock`` advisory lock (``<config-path>.lock``) around the
load→apply→write cycle to prevent TOCTOU races from concurrent PUT calls.

**PR (b) — _DEFAULT_CONFIG import-time env bleed (DONE):** ``_DEFAULT_CONFIG``
is now built from plain-model sub-model defaults (``ControllerConfig()``,
``AdvisorConfig()``, ``SafetyLimits()``) assembled via ``AppConfig.model_construct``,
bypassing ``BaseSettings`` env reads so that ``ConfigFieldMeta.default`` always
reflects schema defaults, not env-injected values.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field

from roastpilot_agent.config import (
    AdvisorConfig,
    AppConfig,
    ControllerConfig,
    LateMaillardTrim,
    MCPDeviceConfig,
    PreFirstCrackLevers,
    SafetyLimits,
)


class ConfigFileError(ValueError):
    """Raised when the saved-config file exists but cannot be parsed.

    Wraps :class:`yaml.YAMLError` with a human-readable message that includes
    the file path and parse reason so that startup fails loud and actionably
    rather than on a raw traceback.  Also raised for a well-formed YAML file
    whose top-level value is not a mapping.
    """


# ---------------------------------------------------------------------------
# Defaults — built from plain-model defaults, never from os.environ.
#
# AppConfig is a BaseSettings subclass and reads ROASTPILOT_* env vars on
# construction.  Building it here at import time would bleed any env var
# that happens to be set into every ConfigFieldMeta.default, making the
# "default" field misleadingly show the env value instead of the schema
# default.
#
# Fix: construct each sub-model (ControllerConfig, AdvisorConfig,
# SafetyLimits) directly — they are plain BaseModel, not BaseSettings, so
# they do not read os.environ — then assemble into an AppConfig via
# model_construct, bypassing the BaseSettings env-read path entirely.
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = AppConfig.model_construct(
    controller=ControllerConfig(),
    advisor=AdvisorConfig(),
    safety=SafetyLimits(),
    mcp_device=MCPDeviceConfig(),
    # mcp and logging omitted intentionally: their default_factory values are
    # populated on attribute access / model_dump, so they are never bleed-prone
    # and are not referenced in build_config_snapshot.
)

#: The default file location when the operator does not override it. Lives in
#: the user's home directory so it survives agent upgrades and does not litter
#: the working directory.  Override via ``ROASTPILOT_CONFIG_FILE=<path>``.
DEFAULT_CONFIG_FILE_PATH = Path.home() / ".roastpilot" / "config.yaml"

# ---------------------------------------------------------------------------
# Read-only env-key guard
# ---------------------------------------------------------------------------

# Env-var prefix for ALL SafetyLimits fields.  The entire prefix is blocked
# at the section level in _inject_saved_as_env — field-list-independent, so
# a new SafetyLimits field can never slip through without a code change to
# AppConfigEdit or SafetyLimitsSnapshot (both intentionally exclude safety).
_SAFETY_ENV_PREFIX: str = "ROASTPILOT_SAFETY__"

# Derive all known ROASTPILOT_SAFETY__* env-var names from the real model so
# tests can assert completeness against SafetyLimits.model_fields (all 10).
# These are INFORMATIONAL (tests + comments); the injection guard operates at
# the section-prefix level above and does not enumerate individual fields.
_ALL_SAFETY_ENV_KEYS: frozenset[str] = frozenset(
    f"{_SAFETY_ENV_PREFIX}{name.upper()}" for name in SafetyLimits.model_fields
)

# Non-safety individual read-only env-var keys — fields whose env vars must
# not be injected from the saved-config file even though they live outside the
# safety section.
#
# Each field name is validated against its model's model_fields at import time
# via an explicit ``assert`` so that a rename raises immediately rather than
# silently producing an empty set (the silent-drop class that caused the
# round-2 BLOCKER is explicitly excluded here).
#
# Current M1 entries and rationale:
#   - controller.tick_interval_seconds: hardware-pinned Hottop polling rate.
#   - advisor.api_key_env: the env-var *name* that holds the API key.
#     Injecting this would silently redirect where the advisor reads the key,
#     contradicting the "the key itself is never stored in config" contract.
#     api_key_env has no env_var in the snapshot (env_var=None → saved=None
#     always) precisely because it must never come from the saved file.
assert "tick_interval_seconds" in ControllerConfig.model_fields, (
    "ControllerConfig.tick_interval_seconds was renamed — update _NEVER_INJECT_NON_SAFETY_KEYS"
)
assert "api_key_env" in AdvisorConfig.model_fields, (
    "AdvisorConfig.api_key_env was renamed — update _NEVER_INJECT_NON_SAFETY_KEYS"
)
_NEVER_INJECT_NON_SAFETY_KEYS: frozenset[str] = frozenset(
    {
        "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS",
        "ROASTPILOT_ADVISOR__API_KEY_ENV",
    }
)

# Guards the snapshot→inject→construct→restore block inside load_app_config().
# asyncio.to_thread() runs concurrent calls on separate OS threads; without the
# lock, one thread could snapshot the other's injected ROASTPILOT_* keys and
# restore them away, producing a race on os.environ (global mutable state).
_ENV_INJECTION_LOCK: threading.Lock = threading.Lock()


def _config_file_path() -> Path:
    """Return the effective saved-config file path.

    Reads ``ROASTPILOT_CONFIG_FILE`` from the environment; falls back to
    :data:`DEFAULT_CONFIG_FILE_PATH`.

    Returns:
        The path to the saved-config YAML file.
    """
    env_val = os.environ.get("ROASTPILOT_CONFIG_FILE")
    return Path(env_val) if env_val else DEFAULT_CONFIG_FILE_PATH


# ---------------------------------------------------------------------------
# Per-field metadata models
# ---------------------------------------------------------------------------


class ConfigFieldMeta(BaseModel, frozen=True):
    """Metadata for a single managed config field (D78, ``GET /api/config`` shape).

    This is the per-field entry returned by ``GET /api/config``.  It carries
    everything the FE needs to render a field: the value the operator saved, the
    value the agent is actually using (env wins), the schema default, and the
    flags that control the UI affordances (read-only = no input; env_overridden
    = badge).

    The type annotation ``Any`` for the value fields is intentional: the HTTP
    response serialises them as JSON via Pydantic, so concrete values (``float``,
    ``int``, ``str``, ``bool``) round-trip correctly. The FE knows each field's
    type from the ``description`` and the ``default``.
    """

    #: The value the operator last saved to the config file, or ``None`` if
    #: the field has never been written (still at the schema default).
    saved_value: Any
    #: The value the agent is currently using. This is the env-variable value
    #: when ``env_overridden`` is ``True``, otherwise the saved value if
    #: present, otherwise the schema default.
    effective_value: Any
    #: The schema default for this field.
    default: Any
    #: ``True`` when a ``ROASTPILOT_*`` env var shadows the saved (or default)
    #: value. The FE renders an "overridden by env" badge.
    env_overridden: bool
    #: ``True`` for fields that may not appear in a ``PUT /api/config`` body.
    #: All :class:`~roastpilot_agent.config.SafetyLimits` values are read-only
    #: in M1 (D78 constraint 2), as are controller tick, FC model, and the
    #: API-key env-var name.
    read_only: bool
    #: Human-readable description shown in the Config UI.
    description: str


# ---------------------------------------------------------------------------
# The full config snapshot returned by GET /api/config
# ---------------------------------------------------------------------------


class ControllerConfigSnapshot(BaseModel, frozen=True):
    """Per-field snapshot for :class:`~roastpilot_agent.config.ControllerConfig`."""

    tick_interval_seconds: ConfigFieldMeta
    pre_fc_heat_target_percent: ConfigFieldMeta
    pre_fc_fan_target_percent: ConfigFieldMeta
    late_maillard_trim_enabled: ConfigFieldMeta
    late_maillard_trim_heat_percent: ConfigFieldMeta
    late_maillard_trim_window_fc_eta_seconds: ConfigFieldMeta
    late_maillard_trim_min_bean_temp_c: ConfigFieldMeta
    late_maillard_trim_adaptive_depth_enabled: ConfigFieldMeta
    late_maillard_trim_base_trim: ConfigFieldMeta
    late_maillard_trim_k_ror: ConfigFieldMeta
    late_maillard_trim_k_eta: ConfigFieldMeta
    late_maillard_trim_ror_ref: ConfigFieldMeta
    late_maillard_trim_eta_ref: ConfigFieldMeta
    late_maillard_trim_min_trim: ConfigFieldMeta
    late_maillard_trim_max_trim: ConfigFieldMeta
    late_maillard_trim_trim_depth_deadband_pp: ConfigFieldMeta
    late_maillard_trim_trim_depth_slew_pp_per_tick: ConfigFieldMeta


class AdvisorConfigSnapshot(BaseModel, frozen=True):
    """Per-field snapshot for :class:`~roastpilot_agent.config.AdvisorConfig`."""

    model_slug: ConfigFieldMeta
    prompt_version: ConfigFieldMeta
    provider: ConfigFieldMeta
    provider_base_url: ConfigFieldMeta
    api_key_env: ConfigFieldMeta
    timeout_seconds: ConfigFieldMeta
    temperature: ConfigFieldMeta


class SafetyLimitsSnapshot(BaseModel, frozen=True):
    """Per-field snapshot for :class:`~roastpilot_agent.config.SafetyLimits`.

    All fields are read-only in M1 (D78 constraint 2): there is no PUT path to
    any safety limit, so the UI can show explanations and defaults without the
    risk of silent weakening.

    All 10 :class:`~roastpilot_agent.config.SafetyLimits` fields are present,
    including the three enforced pre-T0 overrun fields.
    ``pre_t0_overrun_severity`` is a ``Literal["recovery", "fault"]`` displayed
    as a plain string; no special FE handling is required beyond showing the
    value (it is read-only in M1).
    """

    max_bean_temp_c: ConfigFieldMeta
    max_env_temp_c: ConfigFieldMeta
    pre_t0_max_bean_temp_c: ConfigFieldMeta
    overrun_safe_fan_percent: ConfigFieldMeta
    pre_t0_overrun_severity: ConfigFieldMeta
    min_seconds_between_commands: ConfigFieldMeta
    max_consecutive_mcp_failures: ConfigFieldMeta
    max_consecutive_advisor_failures: ConfigFieldMeta
    bitter_ceiling_temp_c: ConfigFieldMeta
    emergency_drop_temp_c: ConfigFieldMeta


class MCPDeviceConfigSnapshot(BaseModel, frozen=True):
    """Per-field snapshot for the managed MCP device fields (D78-4, #420).

    These fields are rendered into the ``coffee-roaster-mcp.yaml`` on each
    (re)spawn.  Environment overrides come from ``ROASTPILOT_MCP_DEVICE__*``.
    The ``mcp_yaml_source_path`` field is **not** included in the snapshot
    (it is a runtime path, not a user-facing config value).
    """

    serial_port: ConfigFieldMeta
    roaster_driver: ConfigFieldMeta
    audio_input_device: ConfigFieldMeta
    recording_enabled: ConfigFieldMeta
    recording_autocapture: ConfigFieldMeta
    recording_devices: ConfigFieldMeta
    fc_mode: ConfigFieldMeta
    fc_confidence_threshold: ConfigFieldMeta
    auto_t0_detection_enabled: ConfigFieldMeta
    auto_t0_drop_threshold_c: ConfigFieldMeta


class AppConfigSnapshot(BaseModel, frozen=True):
    """Full per-field snapshot returned by ``GET /api/config`` (D78).

    Sections mirror :class:`~roastpilot_agent.config.AppConfig`; each leaf
    value is a :class:`ConfigFieldMeta` carrying saved / effective / default /
    env_overridden / read_only.
    """

    controller: ControllerConfigSnapshot
    advisor: AdvisorConfigSnapshot
    safety: SafetyLimitsSnapshot
    mcp_device: MCPDeviceConfigSnapshot


# ---------------------------------------------------------------------------
# Editable managed fields (the PUT surface — safety is excluded by D78)
# ---------------------------------------------------------------------------


class LateMaillardTrimEdit(BaseModel):
    """Editable portion of :class:`~roastpilot_agent.config.LateMaillardTrim` (#386)."""

    enabled: bool | None = None
    trim_heat_percent: int | None = Field(default=None, ge=10, le=100)
    window_fc_eta_seconds: float | None = Field(default=None, gt=0)
    min_bean_temp_c: float | None = Field(default=None, gt=0)
    adaptive_depth_enabled: bool | None = None
    base_trim: int | None = Field(default=None, ge=10, le=100)
    k_ror: float | None = Field(default=None, ge=0.0)
    k_eta: float | None = Field(default=None, ge=0.0)
    ror_ref: float | None = Field(default=None, ge=0.0)
    eta_ref: float | None = Field(default=None, gt=0)
    min_trim: int | None = Field(default=None, ge=10, le=100)
    max_trim: int | None = Field(default=None, ge=10, le=100)
    trim_depth_deadband_pp: int | None = Field(default=None, ge=0, le=20)
    trim_depth_slew_pp_per_tick: int | None = Field(default=None, ge=1, le=20)


class PreFirstCrackLeversEdit(BaseModel):
    """Editable portion of :class:`~roastpilot_agent.config.PreFirstCrackLevers`."""

    heat_target_percent: int | None = Field(default=None, ge=0, le=100)
    fan_target_percent: int | None = Field(default=None, ge=0, le=100)
    late_maillard_trim: LateMaillardTrimEdit | None = None


class ControllerConfigEdit(BaseModel):
    """Editable fields of :class:`~roastpilot_agent.config.ControllerConfig`.

    ``tick_interval_seconds`` is **omitted** — it is hardware-pinned and
    read-only in M1.
    """

    pre_first_crack_levers: PreFirstCrackLeversEdit | None = None


class AdvisorConfigEdit(BaseModel):
    """Editable fields of :class:`~roastpilot_agent.config.AdvisorConfig`.

    ``api_key_env`` is read-only (the env var name is informational only;
    the actual key never appears in config). ``provider_base_url`` is
    editable for users who want to point at a different OpenRouter-compatible
    endpoint or a local Ollama.
    """

    model_slug: str | None = Field(default=None, min_length=1)
    prompt_version: str | None = Field(default=None, min_length=1)
    provider: Literal["openai", "anthropic", "google", "ollama", "openai_compatible"] | None = None
    provider_base_url: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class MCPDeviceConfigEdit(BaseModel):
    """Editable MCP device fields for ``PUT /api/config`` (D78-4, #420).

    These are the managed fields rendered into ``coffee-roaster-mcp.yaml``
    on each (re)spawn.  ``mcp_yaml_source_path`` is intentionally excluded —
    it is set by the operator at deployment time, not via the UI.
    """

    serial_port: str | None = None
    roaster_driver: str | None = None
    audio_input_device: str | None = None
    recording_enabled: bool | None = None
    recording_autocapture: bool | None = None
    recording_devices: list[str] | None = None
    fc_mode: Literal["disabled", "audio", "manual"] | None = None
    fc_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_t0_detection_enabled: bool | None = None
    auto_t0_drop_threshold_c: float | None = Field(default=None, gt=0)


class AppConfigEdit(BaseModel):
    """The PUT /api/config request body — safety is intentionally excluded (D78).

    Only editable managed fields appear here. The server merges them into the
    saved-config file; the next roast picks them up.
    """

    controller: ControllerConfigEdit | None = None
    advisor: AdvisorConfigEdit | None = None
    mcp_device: MCPDeviceConfigEdit | None = None


# ---------------------------------------------------------------------------
# Saved-config file: read / write
# ---------------------------------------------------------------------------

#: Type alias for the raw saved-config dict (YAML-loaded, str keys).
_RawSavedConfig = dict[str, Any]


def _load_saved_config(path: Path) -> _RawSavedConfig:
    """Load the saved-config YAML from *path*, returning an empty dict if absent.

    Args:
        path: The path to the saved-config YAML file.

    Returns:
        The raw dict loaded from the file, or an empty dict if the file does
        not exist or is empty.

    Raises:
        ConfigFileError: If the file exists but cannot be parsed as valid YAML,
            or if the top-level YAML value is not a mapping.  The error message
            includes the file path and the underlying parse reason so the
            operator can locate and fix the file.
        OSError: If the file exists but cannot be read.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigFileError(
            f"Saved config at {path!s} could not be parsed as YAML: {exc}."
            " Fix or remove the file to start the agent."
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigFileError(
            f"Saved config at {path!s} must be a YAML mapping (got"
            f" {type(loaded).__name__!r}). Fix or remove the file to start the agent."
        )
    return loaded  # type: ignore[return-value]


def _write_saved_config(path: Path, data: _RawSavedConfig) -> None:
    """Write *data* to the saved-config YAML at *path*, creating parent dirs.

    The write is **atomic**: YAML is written to a sibling temp file in the same
    directory, flushed to the OS buffer, then renamed over *path* via
    :func:`os.replace`.  On POSIX this is an atomic rename so a concurrent
    ``GET /api/config`` that reads the file never sees a torn/partial YAML.
    The temp file is created by :func:`tempfile.NamedTemporaryFile` with
    ``delete=False`` and cleaned up on error.

    Args:
        path: The path to the saved-config YAML file.
        data: The dict to serialise as YAML. Only editable managed fields
            should appear here — safety is excluded by the caller.

    Raises:
        OSError: If the file or its parent directories cannot be written.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".config-tmp-",
            suffix=".yaml",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            yaml.safe_dump(data, tmp, default_flow_style=False, sort_keys=True)
            tmp.flush()
        # Atomic rename: readers never see a partial file.
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Env-overrides-file resolution
# ---------------------------------------------------------------------------


def _env_is_set(env_key: str) -> bool:
    """Return True when *env_key* is present in the current environment.

    Args:
        env_key: The environment variable name to check.

    Returns:
        ``True`` if the variable is present (even if empty), ``False`` otherwise.
    """
    return env_key in os.environ


def _make_field_meta(
    *,
    saved_value: Any,
    effective_value: Any,
    default_value: Any,
    env_var: str | None,
    read_only: bool,
    description: str,
    injected_keys: frozenset[str] | None = None,
) -> ConfigFieldMeta:
    """Construct a :class:`ConfigFieldMeta` for one managed field.

    Args:
        saved_value: The value from the saved-config file (``None`` if absent).
        effective_value: The value the agent is currently using (env > saved > default).
        default_value: The schema default.
        env_var: The environment variable that overrides this field, or ``None``
            when no single env var maps to it (e.g. nested dict fields).
        read_only: Whether this field appears in the PUT surface.
        description: Human-readable label for the Config UI.
        injected_keys: The set of env-var keys that were injected from the saved
            file by :func:`_inject_saved_as_env`.  A field whose env var is in
            this set is NOT considered env-overridden — the env var was set by
            the injection mechanism on behalf of the saved value, not by an
            operator-supplied env var.  Pass ``None`` (the default) when
            building a snapshot outside of :func:`load_app_config` context
            (e.g. in tests that construct :class:`AppConfig` manually).

    Returns:
        A frozen :class:`ConfigFieldMeta` instance.
    """
    injected = injected_keys or frozenset()
    # A field is env-overridden when either:
    # (a) its own per-field scalar env var is set and not merely injected from
    #     the saved file (e.g. ROASTPILOT_ADVISOR__MODEL_SLUG in os.environ), OR
    # (b) a top-level JSON-blob env var for the section is set AND this field's
    #     own key appears in that blob (e.g. ROASTPILOT_ADVISOR='{"model_slug":
    #     "gpt-4o"}' — model_slug is in the blob, env_overridden=True).
    #     Fields NOT in the blob are NOT flagged — partial-blob fix (#426).
    scalar_overridden = bool(env_var and _env_is_set(env_var) and env_var not in injected)
    section_key = env_var.split("__")[0] if env_var and "__" in env_var else None
    blob_overridden = False
    if section_key and _env_is_set(section_key) and section_key not in injected:
        # The section has a JSON-blob env var.  Parse it and check whether this
        # field's own key is present.  env_var = "ROASTPILOT_ADVISOR__MODEL_SLUG":
        #   section_key = "ROASTPILOT_ADVISOR"
        #   field_key   = "MODEL_SLUG"   (first segment after stripping section__)
        # Compare case-insensitively against the blob's top-level keys.
        field_key_raw = env_var[len(section_key) + 2 :] if env_var else ""
        top_field_key = field_key_raw.split("__")[0].lower()
        import json as _json

        try:
            parsed = _json.loads(os.environ[section_key])
            if isinstance(parsed, dict):
                parsed_dict = cast("dict[str, Any]", parsed)
                # Compare exact snake_case keys — pydantic only consumes
                # snake_case keys, so an uppercase blob key (e.g. "MODEL_SLUG")
                # would not match and correctly leaves blob_overridden=False
                # (#426 P2-B: uppercase keys are ignored by pydantic).
                blob_overridden = top_field_key in parsed_dict
        except (ValueError, TypeError):
            pass  # malformed blob — leave blob_overridden=False
    env_overridden = scalar_overridden or blob_overridden
    return ConfigFieldMeta(
        saved_value=saved_value,
        effective_value=effective_value,
        default=default_value,
        env_overridden=env_overridden,
        read_only=read_only,
        description=description,
    )


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def _raw_section(raw: _RawSavedConfig, key: str) -> _RawSavedConfig:
    """Return the sub-section dict for *key*, or an empty dict if absent/wrong type.

    Args:
        raw: The parent raw dict.
        key: The section key to look up.

    Returns:
        The sub-section as a ``dict[str, Any]``, or ``{}`` if the key is absent
        or the value is not a dict.
    """
    val = raw.get(key)
    if isinstance(val, dict):
        return cast("_RawSavedConfig", val)
    return {}


def build_config_snapshot(
    effective: AppConfig,
    saved_raw: _RawSavedConfig,
    injected_keys: frozenset[str] | None = None,
) -> AppConfigSnapshot:
    """Build the full :class:`AppConfigSnapshot` for ``GET /api/config``.

    Args:
        effective: The fully-resolved :class:`~roastpilot_agent.config.AppConfig`
            as the running agent sees it — env vars already applied (this is
            what :func:`load_app_config` returns from the env).
        saved_raw: The raw dict loaded from the saved-config YAML file.
        injected_keys: The set of env-var keys injected from the saved file by
            :func:`_inject_saved_as_env`.  Pass the value returned by
            :func:`load_app_config` (via :func:`_inject_saved_as_env`) so that
            ``env_overridden`` correctly distinguishes operator-set env vars
            from values injected from the saved file on behalf of the operator.
            Defaults to ``None`` (treated as an empty set) for standalone use.

    Returns:
        An :class:`AppConfigSnapshot` with per-field metadata for every managed
        field.
    """
    defaults = _DEFAULT_CONFIG

    def _meta(
        saved: Any,
        effective_val: Any,
        default_val: Any,
        env_var: str | None,
        *,
        read_only: bool = False,
        description: str = "",
    ) -> ConfigFieldMeta:
        return _make_field_meta(
            saved_value=saved,
            effective_value=effective_val,
            default_value=default_val,
            env_var=env_var,
            read_only=read_only,
            description=description,
            injected_keys=injected_keys,
        )

    # --- controller section ------------------------------------------------
    ctrl = effective.controller
    ctrl_def = defaults.controller
    ctrl_saved = _raw_section(saved_raw, "controller")
    levers_saved = _raw_section(ctrl_saved, "pre_first_crack_levers")
    trim_saved = _raw_section(levers_saved, "late_maillard_trim")

    levers_def: PreFirstCrackLevers = ctrl_def.pre_first_crack_levers
    trim_def: LateMaillardTrim = levers_def.late_maillard_trim
    levers: PreFirstCrackLevers = ctrl.pre_first_crack_levers
    trim: LateMaillardTrim = levers.late_maillard_trim

    controller_snapshot = ControllerConfigSnapshot(
        tick_interval_seconds=_meta(
            ctrl_saved.get("tick_interval_seconds"),
            ctrl.tick_interval_seconds,
            ctrl_def.tick_interval_seconds,
            "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS",
            read_only=True,
            description=(
                "Controller tick interval (seconds). Hardware-pinned to 1.0 s by"
                " the Hottop thermocouple response time — not editable."
            ),
        ),
        pre_fc_heat_target_percent=_meta(
            levers_saved.get("heat_target_percent"),
            levers.heat_target_percent,
            levers_def.heat_target_percent,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT",
            description=(
                "Pre-first-crack heat level (%). The controller holds this value"
                " deterministically from charge to first crack. Default 100."
            ),
        ),
        pre_fc_fan_target_percent=_meta(
            levers_saved.get("fan_target_percent"),
            levers.fan_target_percent,
            levers_def.fan_target_percent,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__FAN_TARGET_PERCENT",
            description=(
                "Pre-first-crack fan level (%). Held deterministically from charge"
                " to first crack. Default 30 (low airflow until browning)."
            ),
        ),
        # --- late Maillard trim ------------------------------------------
        # Pydantic-settings resolves nested models via the double-underscore
        # delimiter, so each nested field has a full env-var path.  Supplying
        # these here makes env_overridden=True when the operator sets the var
        # directly (e.g. in roast-live.sh), matching the D78-1 badge contract.
        late_maillard_trim_enabled=_meta(
            trim_saved.get("enabled"),
            trim.enabled,
            trim_def.enabled,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ENABLED",
            description=(
                "Enable the anticipatory heat trim in the late-Maillard → FC window."
                " When off, the flat heat floor (100 %) is used to FC."
            ),
        ),
        late_maillard_trim_heat_percent=_meta(
            trim_saved.get("trim_heat_percent"),
            trim.trim_heat_percent,
            trim_def.trim_heat_percent,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_HEAT_PERCENT",
            description=(
                "Trimmed heat level (%) held once the late-Maillard window opens."
                " Default 65 — a moderate reduction from 100 %, not a stall."
            ),
        ),
        late_maillard_trim_window_fc_eta_seconds=_meta(
            trim_saved.get("window_fc_eta_seconds"),
            trim.window_fc_eta_seconds,
            trim_def.window_fc_eta_seconds,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__WINDOW_FC_ETA_SECONDS",
            description=(
                "Seconds before the predicted first crack at which the trim window"
                " opens. Default 60 s (late Maillard, ~1 min ahead of the crack)."
            ),
        ),
        late_maillard_trim_min_bean_temp_c=_meta(
            trim_saved.get("min_bean_temp_c"),
            trim.min_bean_temp_c,
            trim_def.min_bean_temp_c,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MIN_BEAN_TEMP_C",
            description=(
                "Minimum bean temperature (°C) below which the trim never engages,"
                " even if FC-ETA projects a near-term crack. Default 155 °C."
            ),
        ),
        # --- adaptive trim depth (#386) -----------------------------------
        late_maillard_trim_adaptive_depth_enabled=_meta(
            trim_saved.get("adaptive_depth_enabled"),
            trim.adaptive_depth_enabled,
            trim_def.adaptive_depth_enabled,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ADAPTIVE_DEPTH_ENABLED",
            description=(
                "Enable adaptive trim depth (#386). When enabled, the trim deepens"
                " on hotter approaches (high RoR, short FC-ETA). Default off."
            ),
        ),
        late_maillard_trim_base_trim=_meta(
            trim_saved.get("base_trim"),
            trim.base_trim,
            trim_def.base_trim,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__BASE_TRIM",
            description=(
                "Adaptive-depth baseline trim (%). The formula produces this depth"
                " when both RoR and ETA gain terms are zero. Default 65 — equal to"
                " the fixed trim_heat_percent so enabling adaptive mode without"
                " tuning reproduces the proven fixed depth exactly."
            ),
        ),
        late_maillard_trim_k_ror=_meta(
            trim_saved.get("k_ror"),
            trim.k_ror,
            trim_def.k_ror,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__K_ROR",
            description=(
                "RoR sensitivity (°C/min per pp of trim deepening). Each extra"
                " °C/min above ror_ref deepens the cut by this many pp. Default 1.5."
            ),
        ),
        late_maillard_trim_k_eta=_meta(
            trim_saved.get("k_eta"),
            trim.k_eta,
            trim_def.k_eta,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__K_ETA",
            description=(
                "ETA sensitivity (s per pp of trim deepening). Each 1 s under"
                " eta_ref deepens the cut by this many pp. Default 0.2."
            ),
        ),
        late_maillard_trim_ror_ref=_meta(
            trim_saved.get("ror_ref"),
            trim.ror_ref,
            trim_def.ror_ref,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ROR_REF",
            description=(
                "RoR reference level (°C/min). Below this the RoR term contributes"
                " 0; above it the cut deepens. Default 8.0."
            ),
        ),
        late_maillard_trim_eta_ref=_meta(
            trim_saved.get("eta_ref"),
            trim.eta_ref,
            trim_def.eta_ref,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ETA_REF",
            description=(
                "ETA reference (seconds). The ETA term is 0 at the window boundary"
                " and deepens only when FC is closer than this. Default 60.0."
            ),
        ),
        late_maillard_trim_min_trim=_meta(
            trim_saved.get("min_trim"),
            trim.min_trim,
            trim_def.min_trim,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MIN_TRIM",
            description=(
                "Deepest permitted adaptive trim (%). The formula cannot go below"
                " this, preventing stall of first crack. Default 45."
            ),
        ),
        late_maillard_trim_max_trim=_meta(
            trim_saved.get("max_trim"),
            trim.max_trim,
            trim_def.max_trim,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MAX_TRIM",
            description=(
                "Shallowest permitted adaptive trim (%). The formula cannot go above"
                " this (adaptive depth is always a reduction). Default 75."
            ),
        ),
        late_maillard_trim_trim_depth_deadband_pp=_meta(
            trim_saved.get("trim_depth_deadband_pp"),
            trim.trim_depth_deadband_pp,
            trim_def.trim_depth_deadband_pp,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_DEPTH_DEADBAND_PP",
            description=(
                "Adaptive-depth tick-to-tick deadband (pp). Changes smaller than"
                " this are suppressed to avoid noise-driven micro-adjustments."
                " Must be strictly less than trim_depth_slew_pp_per_tick. Default 2."
            ),
        ),
        late_maillard_trim_trim_depth_slew_pp_per_tick=_meta(
            trim_saved.get("trim_depth_slew_pp_per_tick"),
            trim.trim_depth_slew_pp_per_tick,
            trim_def.trim_depth_slew_pp_per_tick,
            "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_DEPTH_SLEW_PP_PER_TICK",
            description=(
                "Adaptive-depth maximum change per accepted write (pp). Limits how"
                " fast the trim depth can move between consecutive accepted MCP"
                " writes, damping thrash from jittery RoR. Must be strictly greater"
                " than trim_depth_deadband_pp. Default 3."
            ),
        ),
    )

    # --- advisor section ---------------------------------------------------
    adv = effective.advisor
    adv_def = defaults.advisor
    adv_saved = _raw_section(saved_raw, "advisor")

    advisor_snapshot = AdvisorConfigSnapshot(
        model_slug=_meta(
            adv_saved.get("model_slug"),
            adv.model_slug,
            adv_def.model_slug,
            "ROASTPILOT_ADVISOR__MODEL_SLUG",
            description=(
                "The advisor model slug (provider/model-id via OpenRouter)."
                " Default 'openai/gpt-4o' (#277 bake-off pin)."
            ),
        ),
        prompt_version=_meta(
            adv_saved.get("prompt_version"),
            adv.prompt_version,
            adv_def.prompt_version,
            "ROASTPILOT_ADVISOR__PROMPT_VERSION",
            description=(
                "Control-teaching prompt version. c3 is the live default."
                " c4/c5/c6 are opt-in experiment selectors (see #396)."
            ),
        ),
        provider=_meta(
            adv_saved.get("provider"),
            adv.provider,
            adv_def.provider,
            "ROASTPILOT_ADVISOR__PROVIDER",
            description=(
                "Advisor provider. 'openai_compatible' uses OpenRouter (default)."
                " Use 'openai' / 'anthropic' / 'google' for native providers."
            ),
        ),
        provider_base_url=_meta(
            adv_saved.get("provider_base_url"),
            adv.provider_base_url,
            adv_def.provider_base_url,
            "ROASTPILOT_ADVISOR__PROVIDER_BASE_URL",
            description=(
                "Base URL for the OpenAI-compatible provider endpoint. Default"
                " is the OpenRouter API URL; override for Ollama or other endpoints."
            ),
        ),
        api_key_env=_meta(
            None,  # never read from file; always informational
            adv.api_key_env,
            adv_def.api_key_env,
            None,
            read_only=True,
            description=(
                "The environment variable that holds the advisor API key."
                " The key itself is never stored in config — set this env var."
            ),
        ),
        timeout_seconds=_meta(
            adv_saved.get("timeout_seconds"),
            adv.timeout_seconds,
            adv_def.timeout_seconds,
            "ROASTPILOT_ADVISOR__TIMEOUT_SECONDS",
            description=(
                "Per-call advisor timeout (seconds). The controller must not block"
                " the tick loop past this; Default 10.0 s."
            ),
        ),
        temperature=_meta(
            adv_saved.get("temperature"),
            adv.temperature,
            adv_def.temperature,
            "ROASTPILOT_ADVISOR__TEMPERATURE",
            description=(
                "Sampling temperature for the advisor model. 0.0 (default) = fully"
                " deterministic; higher values add stochasticity."
            ),
        ),
    )

    # --- safety section (all read-only in M1) -----------------------------
    sf = effective.safety
    sf_def = defaults.safety

    safety_snapshot = SafetyLimitsSnapshot(
        max_bean_temp_c=_meta(
            None,
            sf.max_bean_temp_c,
            sf_def.max_bean_temp_c,
            "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C",
            read_only=True,
            description=(
                "Hard bean-temperature ceiling (°C). The safety box faults above"
                " this. Default 230 °C — above second-crack territory. Read-only."
            ),
        ),
        max_env_temp_c=_meta(
            None,
            sf.max_env_temp_c,
            sf_def.max_env_temp_c,
            "ROASTPILOT_SAFETY__MAX_ENV_TEMP_C",
            read_only=True,
            description=(
                "Hard environment-temperature ceiling (°C). Readings above this"
                " indicate a fault. Default 240 °C. Read-only."
            ),
        ),
        pre_t0_max_bean_temp_c=_meta(
            None,
            sf.pre_t0_max_bean_temp_c,
            sf_def.pre_t0_max_bean_temp_c,
            "ROASTPILOT_SAFETY__PRE_T0_MAX_BEAN_TEMP_C",
            read_only=True,
            description=(
                "Maximum bean temperature (°C) permitted during preheating before"
                " T0 (charge) is confirmed. Exceeding this while the charge has not"
                " yet been confirmed triggers the pre-T0 overrun policy (heat 0 %,"
                " fan to overrun_safe_fan_percent). Default 200 °C. Read-only."
            ),
        ),
        overrun_safe_fan_percent=_meta(
            None,
            sf.overrun_safe_fan_percent,
            sf_def.overrun_safe_fan_percent,
            "ROASTPILOT_SAFETY__OVERRUN_SAFE_FAN_PERCENT",
            read_only=True,
            description=(
                "Fan level (%) applied during a pre-T0 overrun. Keeps the chamber"
                " ventilated while heat is cut to 0 %. Default 100 (maximum"
                " airflow). Read-only."
            ),
        ),
        pre_t0_overrun_severity=_meta(
            None,
            sf.pre_t0_overrun_severity,
            sf_def.pre_t0_overrun_severity,
            "ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY",
            read_only=True,
            description=(
                "Severity of a pre-T0 temperature overrun: 'recovery' (operator"
                " must acknowledge before resuming) or 'fault' (immediate halt)."
                " Default 'recovery'. Displayed as a plain string. Read-only."
            ),
        ),
        min_seconds_between_commands=_meta(
            None,
            sf.min_seconds_between_commands,
            sf_def.min_seconds_between_commands,
            "ROASTPILOT_SAFETY__MIN_SECONDS_BETWEEN_COMMANDS",
            read_only=True,
            description=(
                "Minimum seconds between roaster commands. The Hottop serial loop"
                " runs at ~1 Hz; writes more frequent than this have no effect."
                " Default 2.0 s. Read-only."
            ),
        ),
        max_consecutive_mcp_failures=_meta(
            None,
            sf.max_consecutive_mcp_failures,
            sf_def.max_consecutive_mcp_failures,
            "ROASTPILOT_SAFETY__MAX_CONSECUTIVE_MCP_FAILURES",
            read_only=True,
            description=(
                "Consecutive MCP read failures tolerated before a fault."
                " Default 3 (~3 s at 1 Hz tick). Read-only."
            ),
        ),
        max_consecutive_advisor_failures=_meta(
            None,
            sf.max_consecutive_advisor_failures,
            sf_def.max_consecutive_advisor_failures,
            "ROASTPILOT_SAFETY__MAX_CONSECUTIVE_ADVISOR_FAILURES",
            read_only=True,
            description=(
                "Consecutive advisor availability failures before failing closed."
                " Default 3. Does not count malformed/unsafe responses. Read-only."
            ),
        ),
        bitter_ceiling_temp_c=_meta(
            None,
            sf.bitter_ceiling_temp_c,
            sf_def.bitter_ceiling_temp_c,
            "ROASTPILOT_SAFETY__BITTER_CEILING_TEMP_C",
            read_only=True,
            description=(
                "Drop/bitter ceiling (°C). Bean temperature past which a medium"
                " roast turns bitter. Advisor and control are told this ceiling."
                " Default 196 °C. Read-only."
            ),
        ),
        emergency_drop_temp_c=_meta(
            None,
            sf.emergency_drop_temp_c,
            sf_def.emergency_drop_temp_c,
            "ROASTPILOT_SAFETY__EMERGENCY_DROP_TEMP_C",
            read_only=True,
            description=(
                "Emergency-drop temperature (°C). Above this the roast must be"
                " dropped immediately regardless of development. 2 °C above the"
                " bitter ceiling by design. Default 198 °C. Read-only."
            ),
        ),
    )

    # --- mcp_device section (D78-4, #420) ------------------------------------
    dev = effective.mcp_device
    dev_def = defaults.mcp_device
    dev_saved = _raw_section(saved_raw, "mcp_device")

    mcp_device_snapshot = MCPDeviceConfigSnapshot(
        serial_port=_meta(
            dev_saved.get("serial_port"),
            dev.serial_port,
            dev_def.serial_port,
            "ROASTPILOT_MCP_DEVICE__SERIAL_PORT",
            description=(
                "Serial port path for the Hottop roaster"
                " (e.g. /dev/cu.usbserial-XXXXXXXX on macOS,"
                " /dev/ttyUSB0 on Linux). Rendered to roaster.port in the"
                " MCP yaml on each spawn."
            ),
        ),
        roaster_driver=_meta(
            dev_saved.get("roaster_driver"),
            dev.roaster_driver,
            dev_def.roaster_driver,
            "ROASTPILOT_MCP_DEVICE__ROASTER_DRIVER",
            description=(
                "coffee-roaster-mcp driver name"
                " (e.g. hottop_kn8828b_2k_plus or mock). Rendered to"
                " roaster.driver in the MCP yaml."
            ),
        ),
        audio_input_device=_meta(
            dev_saved.get("audio_input_device"),
            dev.audio_input_device,
            dev_def.audio_input_device,
            "ROASTPILOT_MCP_DEVICE__AUDIO_INPUT_DEVICE",
            description=(
                "PortAudio input device name substring for FC detection"
                " (case-insensitive match, e.g. 'USB PnP'). Rendered to"
                " audio.input_device in the MCP yaml."
            ),
        ),
        recording_enabled=_meta(
            dev_saved.get("recording_enabled"),
            dev.recording_enabled,
            dev_def.recording_enabled,
            "ROASTPILOT_MCP_DEVICE__RECORDING_ENABLED",
            description=(
                "Whether the MCP audio recorder is active. Rendered to"
                " recording.enabled in the MCP yaml."
            ),
        ),
        recording_autocapture=_meta(
            dev_saved.get("recording_autocapture"),
            dev.recording_autocapture,
            dev_def.recording_autocapture,
            "ROASTPILOT_MCP_DEVICE__RECORDING_AUTOCAPTURE",
            description=(
                "Whether recording starts automatically with each roast session."
                " Rendered to recording.autocapture in the MCP yaml."
            ),
        ),
        recording_devices=_meta(
            dev_saved.get("recording_devices"),
            dev.recording_devices,
            dev_def.recording_devices,
            "ROASTPILOT_MCP_DEVICE__RECORDING_DEVICES",
            description=(
                "Ordered list of capture-device name substrings. The first entry"
                " is used by the FC detector (teed); additional entries are"
                " independent capture streams. Rendered to recording.devices."
            ),
        ),
        fc_mode=_meta(
            dev_saved.get("fc_mode"),
            dev.fc_mode,
            dev_def.fc_mode,
            "ROASTPILOT_MCP_DEVICE__FC_MODE",
            description=(
                "First-crack detection mode: disabled, audio, or manual. Rendered"
                " to first_crack.mode in the MCP yaml."
            ),
        ),
        fc_confidence_threshold=_meta(
            dev_saved.get("fc_confidence_threshold"),
            dev.fc_confidence_threshold,
            dev_def.fc_confidence_threshold,
            "ROASTPILOT_MCP_DEVICE__FC_CONFIDENCE_THRESHOLD",
            description=(
                "FC detector confidence threshold [0, 1]. Lower = more sensitive;"
                " 0.6 is the proven library default. Rendered to"
                " first_crack.confidence_threshold in the MCP yaml."
            ),
        ),
        auto_t0_detection_enabled=_meta(
            dev_saved.get("auto_t0_detection_enabled"),
            dev.auto_t0_detection_enabled,
            dev_def.auto_t0_detection_enabled,
            "ROASTPILOT_MCP_DEVICE__AUTO_T0_DETECTION_ENABLED",
            description=(
                "Whether the MCP's automatic charge-drop (T0) detection is active."
                " Rendered to session.auto_t0_detection_enabled in the MCP yaml."
            ),
        ),
        auto_t0_drop_threshold_c=_meta(
            dev_saved.get("auto_t0_drop_threshold_c"),
            dev.auto_t0_drop_threshold_c,
            dev_def.auto_t0_drop_threshold_c,
            "ROASTPILOT_MCP_DEVICE__AUTO_T0_DROP_THRESHOLD_C",
            description=(
                "Bean-temperature drop (°C) that triggers automatic T0 detection."
                " Rendered to session.auto_t0_drop_threshold_c in the MCP yaml."
            ),
        ),
    )

    return AppConfigSnapshot(
        controller=controller_snapshot,
        advisor=advisor_snapshot,
        safety=safety_snapshot,
        mcp_device=mcp_device_snapshot,
    )


# ---------------------------------------------------------------------------
# Merge edit → saved file
# ---------------------------------------------------------------------------


def apply_config_edit(
    edit: AppConfigEdit,
    existing_saved: _RawSavedConfig,
) -> _RawSavedConfig:
    """Merge an :class:`AppConfigEdit` into the existing saved-config dict.

    Only fields explicitly set (non-``None``) in *edit* are written; fields
    omitted in *edit* remain untouched in *existing_saved*. Safety is
    intentionally excluded — calling code must never pass safety values here,
    and this function has no path to :class:`~roastpilot_agent.config.SafetyLimits`.

    Args:
        edit: The validated edit from a ``PUT /api/config`` request.
        existing_saved: The current saved-config dict loaded from the file.

    Returns:
        A new dict merging *edit*'s non-``None`` fields into *existing_saved*.

    Raises:
        ValueError: If the merged result would fail
            :class:`~roastpilot_agent.config.AppConfig` schema validation.
    """
    import copy

    merged: _RawSavedConfig = copy.deepcopy(existing_saved)

    if edit.controller is not None:
        ctrl_section: dict[str, Any] = merged.setdefault("controller", {})
        c = edit.controller
        if c.pre_first_crack_levers is not None:
            levers_section: dict[str, Any] = ctrl_section.setdefault("pre_first_crack_levers", {})
            lv = c.pre_first_crack_levers
            if lv.heat_target_percent is not None:
                levers_section["heat_target_percent"] = lv.heat_target_percent
            if lv.fan_target_percent is not None:
                levers_section["fan_target_percent"] = lv.fan_target_percent
            if lv.late_maillard_trim is not None:
                trim_section: dict[str, Any] = levers_section.setdefault("late_maillard_trim", {})
                t = lv.late_maillard_trim
                _merge_non_none(
                    trim_section,
                    {
                        "enabled": t.enabled,
                        "trim_heat_percent": t.trim_heat_percent,
                        "window_fc_eta_seconds": t.window_fc_eta_seconds,
                        "min_bean_temp_c": t.min_bean_temp_c,
                        "adaptive_depth_enabled": t.adaptive_depth_enabled,
                        "base_trim": t.base_trim,
                        "k_ror": t.k_ror,
                        "k_eta": t.k_eta,
                        "ror_ref": t.ror_ref,
                        "eta_ref": t.eta_ref,
                        "min_trim": t.min_trim,
                        "max_trim": t.max_trim,
                        "trim_depth_deadband_pp": t.trim_depth_deadband_pp,
                        "trim_depth_slew_pp_per_tick": t.trim_depth_slew_pp_per_tick,
                    },
                )

    if edit.advisor is not None:
        adv_section: dict[str, Any] = merged.setdefault("advisor", {})
        a = edit.advisor
        _merge_non_none(
            adv_section,
            {
                "model_slug": a.model_slug,
                "prompt_version": a.prompt_version,
                "provider": a.provider,
                "provider_base_url": a.provider_base_url,
                "timeout_seconds": a.timeout_seconds,
                "temperature": a.temperature,
            },
        )

    if edit.mcp_device is not None:
        dev_section: dict[str, Any] = merged.setdefault("mcp_device", {})
        d = edit.mcp_device
        # recording_devices is list[str] | None in the edit model; convert to
        # tuple[str, ...] when writing so it round-trips cleanly through the
        # MCPDeviceConfig (which stores as tuple).  The yaml layer stores it
        # as a list anyway; AppConfig/pydantic converts on load.
        recording_devices_val: tuple[str, ...] | None = (
            tuple(d.recording_devices) if d.recording_devices is not None else None
        )
        # Tri-state inherit/override (#439): use model_fields_set to distinguish
        # "explicitly set to None" (clear → inherit) from "not provided" (skip).
        # A field set to None in the PUT body means "clear back to inherit" — delete
        # the key from the saved section so the hand-authored MCP yaml governs it.
        # A field absent from the PUT body (not in model_fields_set) is skipped.
        # A field set to a non-None value is written as before.
        #
        # Blank-string guard: an empty string for any string device field is not a
        # valid device path/name and would write port:""/driver:""/input_device:""
        # into the MCP yaml — crashing the child on the next spawn.  Treat "" as
        # None (clear → inherit from hand-authored yaml) for all three (#439).
        dev_fields: dict[str, Any] = {
            "serial_port": d.serial_port or None,
            "roaster_driver": d.roaster_driver or None,
            "audio_input_device": d.audio_input_device or None,
            "recording_enabled": d.recording_enabled,
            "recording_autocapture": d.recording_autocapture,
            "recording_devices": recording_devices_val,
            "fc_mode": d.fc_mode,
            "fc_confidence_threshold": d.fc_confidence_threshold,
            "auto_t0_detection_enabled": d.auto_t0_detection_enabled,
            "auto_t0_drop_threshold_c": d.auto_t0_drop_threshold_c,
        }
        explicitly_set = d.model_fields_set
        _merge_device_fields(dev_section, dev_fields, explicitly_set)

    # Validate by constructing AppConfig from the merged saved dict.  We
    # do this before writing so that an invalid edit is rejected at the API
    # layer with a clear validation error rather than silently written to disk.
    _validate_merged_config(merged)

    return merged


def _merge_non_none(target: dict[str, Any], updates: dict[str, Any]) -> None:
    """Update *target* in-place with *updates*, skipping ``None`` values.

    Args:
        target: The dict to update in-place.
        updates: Key/value pairs; keys whose values are ``None`` are skipped.
    """
    for key, val in updates.items():
        if val is not None:
            target[key] = val


def _merge_device_fields(
    target: dict[str, Any],
    fields: dict[str, Any],
    explicitly_set: set[str],
) -> None:
    """Merge MCP device fields with tri-state inherit/override semantics (#439).

    Three outcomes per field:

    - **Not in ``explicitly_set``**: field was absent from the PUT body — skip
      it; whatever is already in the saved section is unchanged.
    - **In ``explicitly_set`` and value is ``None``**: the operator cleared the
      field back to "inherit from hand-authored yaml" — delete the key from
      the saved section so the hand-authored value governs it again.
    - **In ``explicitly_set`` and value is non-``None``**: write the new value
      as an override.

    Args:
        target: The ``mcp_device`` section dict from the saved config, modified
            in-place.
        fields: Mapping of canonical saved-dict key → resolved value (already
            processed, e.g. ``recording_devices`` converted to a tuple, blank
            ``roaster_driver`` replaced with ``None``).
        explicitly_set: The ``model_fields_set`` from the
            :class:`MCPDeviceConfigEdit` Pydantic model — the set of field
            names that were present in the PUT body (even when ``None``).
    """
    for key, val in fields.items():
        if key not in explicitly_set:
            # Absent from the PUT body — leave the saved section unchanged.
            continue
        if val is None:
            # Explicit null → clear: remove from the saved section so the
            # hand-authored MCP yaml (or MCP defaults) governs this field.
            target.pop(key, None)
        else:
            target[key] = val


def _validate_merged_config(saved_raw: _RawSavedConfig) -> None:
    """Validate that *saved_raw* overlaid onto defaults still passes the schema.

    Constructs a throwaway :class:`~roastpilot_agent.config.AppConfig` from the
    merged dict and lets Pydantic raise ``ValidationError`` on any violation.

    Args:
        saved_raw: The merged saved-config dict (before writing to disk).

    Raises:
        pydantic.ValidationError: If the merged values violate the schema.
    """
    from pydantic import TypeAdapter

    # Build from defaults, then overlay the saved dict.
    default_dict = _DEFAULT_CONFIG.model_dump()
    _deep_merge(default_dict, saved_raw)
    # Construct without env (to check the file values in isolation from any
    # current env that might override).  We use model_validate on a plain
    # model, not BaseSettings, to avoid env re-injection.  Raises
    # pydantic.ValidationError on schema violations — let it propagate.
    ta: TypeAdapter[AppConfig] = TypeAdapter(AppConfig)
    ta.validate_python(default_dict)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Merge *overlay* into *base* in-place, recursing into nested dicts.

    Args:
        base: The base dict, modified in-place.
        overlay: Values to layer on top; sub-dicts are merged recursively.
    """
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)  # type: ignore[arg-type]
        else:
            base[key] = val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_app_config() -> tuple[AppConfig, frozenset[str]]:
    """Load the effective :class:`~roastpilot_agent.config.AppConfig`.

    This is the single entry point for the running agent to obtain its
    config.  Resolution order (env-overrides-file, D78):

    1. Schema defaults (built into the Pydantic models).
    2. Saved-config file (``ROASTPILOT_CONFIG_FILE`` or the default path).
    3. ``ROASTPILOT_*`` environment variables (win; parsed by BaseSettings).

    Returns:
        A tuple of:

        - The fully-resolved :class:`~roastpilot_agent.config.AppConfig`.
        - A frozenset of env-var keys that were injected from the saved-config
          file by :func:`_inject_saved_as_env`.  Pass this to
          :func:`build_config_snapshot` so that ``env_overridden`` in each
          :class:`ConfigFieldMeta` correctly distinguishes operator-set env
          vars from values the injection mechanism set on their behalf.

    Raises:
        ConfigFileError: If the saved-config file exists but is malformed.
        OSError: If the saved-config file exists but cannot be read.
    """
    # Step 2: load the saved file into a flat env-like dict that BaseSettings
    # can consume.  We inject the saved values as *environment variables* with
    # the ``ROASTPILOT_`` prefix so BaseSettings' own env-override semantics
    # apply correctly.  Any real ``ROASTPILOT_*`` env var (step 3) already
    # wins because os.environ takes precedence — we only inject for keys that
    # are absent from the real environment.
    #
    # The injection is scoped to this call via a snapshot/restore so that
    # repeated calls to load_app_config() are idempotent and do not
    # permanently mutate os.environ.  Without the restore, a second call
    # after a PUT would see the previously-injected value as an env var,
    # making env_overridden=True for a field that was never set by the
    # operator (cross-request env pollution, D78 PR b Codex finding).
    saved_raw = _load_saved_config(_config_file_path())
    # Serialise the env-injection window so two concurrent asyncio.to_thread()
    # calls cannot interleave their snapshot/inject/restore cycles and corrupt
    # each other's view of os.environ (_ENV_INJECTION_LOCK is module-level).
    with _ENV_INJECTION_LOCK:
        env_snapshot = os.environ.copy()
        try:
            injected_keys = _inject_saved_as_env(saved_raw)
            config = AppConfig()
        finally:
            # Restore os.environ to its pre-injection state.  Keys that existed
            # before are put back; keys that were added by injection are removed.
            for key in list(os.environ):
                if key not in env_snapshot:
                    del os.environ[key]
            for key, val in env_snapshot.items():
                if os.environ.get(key) != val:
                    os.environ[key] = val  # pragma: no cover
    return config, injected_keys


def _inject_saved_as_env(saved_raw: _RawSavedConfig) -> frozenset[str]:
    """Inject saved-config values into ``os.environ`` as ``ROASTPILOT_*`` vars.

    Only keys NOT already present in the environment are injected, so real
    env-var overrides still win (env-overrides-file precedence, D78).

    **Safety guard (field-list-independent):** the entire ``ROASTPILOT_SAFETY__``
    prefix is skipped at the section level — no field in :class:`SafetyLimits`
    can ever be injected from the saved file, regardless of how many fields the
    model has or how a future contributor names them.  This is the primary guard
    and is independent of the field list in :data:`_ALL_SAFETY_ENV_KEYS` (which
    exists for test-completeness assertions, not enforcement).  The environment
    can still override safety limits via explicit ``ROASTPILOT_SAFETY__*`` vars;
    that is a deliberate operator choice, not a silent file path.

    **Non-safety read-only guard:** individual keys listed in
    :data:`_NEVER_INJECT_NON_SAFETY_KEYS` (e.g. controller tick) are also
    skipped at the field level inside :func:`_inject_section`.

    Args:
        saved_raw: The raw dict loaded from the saved-config YAML file.

    Returns:
        The frozenset of environment-variable names that were injected.  Pass
        this to :func:`build_config_snapshot` so that ``env_overridden`` in each
        :class:`ConfigFieldMeta` correctly distinguishes operator-set env vars
        from values injected from the saved file on the operator's behalf.
    """
    injected: set[str] = set()
    for section, section_val in saved_raw.items():
        if not isinstance(section_val, dict):
            continue
        # Normalise to uppercase so that any capitalisation of a section name
        # (e.g. 'Safety', 'SAFETY', 'SaFeTy') maps to the same prefix.
        prefix = f"ROASTPILOT_{section.upper()}__"
        if prefix == _SAFETY_ENV_PREFIX:
            # Skip the entire safety section — field-list-independent guard.
            # A new SafetyLimits field cannot accidentally become injectable
            # without an explicit change to AppConfigEdit or SafetyLimitsSnapshot.
            continue
        # JSON-blob section env var precedence (#426): if the operator has set a
        # top-level JSON-blob env var for this section (e.g.
        # ROASTPILOT_ADVISOR='{"model_slug":"gpt-4o"}'), its fields win over
        # any scalars we inject.  pydantic-settings resolves scalar nested env
        # vars (ROASTPILOT_ADVISOR__MODEL_SLUG) AFTER the JSON blob, so
        # injecting a saved scalar for a field the blob already sets would
        # shadow the blob value — wrong.
        #
        # FIX (partial-blob safe): parse the blob's top-level keys and skip
        # injection ONLY for the fields actually present in the blob.  Fields
        # NOT in the blob don't compete with anything and must keep their saved
        # value (the injected scalar wins because no other var is present).
        # If the blob is malformed or not a dict, fall back to skipping the
        # entire section so we never re-introduce the original shadow bug.
        section_key = prefix[:-2]  # strip trailing "__" → e.g. "ROASTPILOT_ADVISOR"
        blob_fields: frozenset[str] = frozenset()
        blob_dict: dict[str, Any] = {}
        if section_key in os.environ:
            import json as _json

            try:
                parsed = _json.loads(os.environ[section_key])
                if isinstance(parsed, dict):
                    # Keep blob keys as-is (snake_case) — pydantic only consumes
                    # snake_case keys, so uppercasing here would cause an
                    # uppercase-keyed blob field to wrongly skip injection of the
                    # saved value while pydantic silently ignores the bad key
                    # and falls back to the schema default (#426 P2-B).
                    parsed_dict = cast("dict[str, Any]", parsed)
                    blob_dict: dict[str, Any] = parsed_dict
                    blob_fields = frozenset(parsed_dict)
                else:
                    # Non-dict blob (unexpected) — skip the whole section to
                    # avoid injecting scalars that compete with the blob value.
                    continue
            except (ValueError, TypeError):
                # Malformed blob — skip the whole section (safe fallback).
                continue
        _inject_section(
            cast("dict[str, Any]", section_val),
            prefix,
            injected,
            blob_fields=blob_fields,
            blob_dict=blob_dict,
        )
    return frozenset(injected)


def _inject_section(
    section_dict: dict[str, Any],
    prefix: str,
    injected: set[str],
    *,
    blob_fields: frozenset[str] = frozenset(),
    blob_dict: dict[str, Any] | None = None,
) -> None:
    """Recursively inject a section of the saved config as env vars.

    Args:
        section_dict: The section's value dict from the saved-config YAML.
        prefix: The env-var prefix accumulated so far (already uppercased).
        injected: Mutable set to record every env-var key that is written.
        blob_fields: Snake-case top-level keys that a JSON-blob section env var
            already covers for this section (#426).  A saved scalar for a field
            in this set is NOT injected — the blob value wins and injecting a
            competing scalar would shadow it.
        blob_dict: The raw parsed blob dict for this section level.  When a
            key maps to a nested sub-dict in the blob, that sub-dict is passed
            recursively so nested blob fields are also covered (#426 P2-A).
    """
    import json

    effective_blob_dict: dict[str, Any] = blob_dict if blob_dict is not None else {}

    for key, val in section_dict.items():
        env_key = f"{prefix}{key.upper()}"
        if env_key in _NEVER_INJECT_NON_SAFETY_KEYS:
            # This non-safety field is read-only in the snapshot — skip it.
            continue
        if isinstance(val, dict):
            # Nested sub-section: propagate blob coverage if the blob has a
            # matching sub-dict for this key (#426 P2-A).
            nested_blob_val = effective_blob_dict.get(key)
            if isinstance(nested_blob_val, dict):
                nested_blob = cast("dict[str, Any]", nested_blob_val)
                _inject_section(
                    cast("dict[str, Any]", val),
                    f"{env_key}__",
                    injected,
                    blob_fields=frozenset(nested_blob),
                    blob_dict=nested_blob,
                )
            else:
                _inject_section(cast("dict[str, Any]", val), f"{env_key}__", injected)
        elif key in blob_fields:
            # This field is already set by the JSON-blob env var — skip
            # injection so the blob value wins (#426 partial-blob fix).
            pass
        elif env_key not in os.environ:
            # Scalar — serialise to a JSON-compatible string so pydantic-settings
            # can coerce it back (bool → "true"/"false", int/float → numeric str).
            if isinstance(val, bool):
                os.environ[env_key] = "true" if val else "false"
            else:
                os.environ[env_key] = json.dumps(val) if not isinstance(val, str) else val
            injected.add(env_key)


def load_saved_raw() -> _RawSavedConfig:
    """Load the raw saved-config dict from the configured file path.

    This is used by the API layer to build :class:`AppConfigSnapshot` (the
    GET response): it needs the saved dict separately to show ``saved_value``
    vs ``effective_value`` per field.

    Returns:
        The raw saved-config dict, or ``{}`` if the file is absent.

    Raises:
        ConfigFileError: If the file exists but is malformed.
        OSError: If the file exists but cannot be read.
    """
    return _load_saved_config(_config_file_path())


def persist_config_edit(edit: AppConfigEdit) -> None:
    """Validate *edit* and write the result to the saved-config file.

    Merges *edit* into the current saved-config dict, validates the combined
    result against the :class:`~roastpilot_agent.config.AppConfig` schema, and
    writes it back to disk atomically.  Safety limits are excluded by the
    :class:`AppConfigEdit` type — there is simply no field for them.

    The load→apply→write cycle is serialised with a ``filelock`` advisory lock
    (``<config-path>.lock``), so concurrent ``PUT /api/config`` calls — e.g.
    from two browser tabs — cannot interleave their read-modify-write and lose
    one caller's changes (TOCTOU guard, D78 PR b).

    Args:
        edit: The validated edit from a ``PUT /api/config`` request.

    Raises:
        pydantic.ValidationError: If the merged config violates the schema.
        ConfigFileError: If the existing file is malformed (read phase).
        OSError: If the file cannot be written.
    """
    import filelock

    path = _config_file_path()
    # Ensure the parent directory exists before acquiring the lock file.
    # On first save the directory (~/.roastpilot/ by default) may not exist;
    # filelock raises OSError creating the .lock file if the parent is absent.
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with filelock.FileLock(lock_path):
        existing = _load_saved_config(path)
        merged = apply_config_edit(edit, existing)
        _write_saved_config(path, merged)
