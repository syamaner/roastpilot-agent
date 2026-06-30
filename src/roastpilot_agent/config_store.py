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

**TODO (PR b) — file-lock around persist_config_edit:** Add a ``filelock``
(or ``fcntl.flock``) around the read-modify-write in :func:`persist_config_edit`
before the PUT endpoint goes live; the current code has a TOCTOU window between
:func:`_load_saved_config` and :func:`_write_saved_config`.

**TODO (PR b) — _DEFAULT_CONFIG import-time env bleed:** ``_DEFAULT_CONFIG =
AppConfig()`` at module import time reads the current ``os.environ``, so the
``default`` field in each :class:`ConfigFieldMeta` will reflect any
``ROASTPILOT_*`` env var that happens to be set when the module is first
imported (e.g. in ``roast-live.sh``). Fix by deriving ``default`` from
``AppConfig.model_fields`` field defaults directly, rather than from a
pre-constructed instance.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field

from roastpilot_agent.config import (
    AppConfig,
    LateMaillardTrim,
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
# Defaults — a single frozen instance, so we compare without re-constructing.
# TODO (PR b): _DEFAULT_CONFIG reads os.environ at import time — any ROASTPILOT_*
#   env var set when the module is first imported will bleed into the `default`
#   field of every ConfigFieldMeta.  Fix by deriving defaults from model_fields
#   directly rather than from a pre-constructed AppConfig() instance.
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = AppConfig()

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

# Non-safety individual read-only env-var keys — fields that are read_only in
# the snapshot but live in non-safety sections.  Derived from the known set so
# adding a new read-only controller/advisor field requires updating this set.
# Driven by metadata: each entry maps 1:1 to a ConfigFieldMeta with
# read_only=True and a known env_var in build_config_snapshot.
_NEVER_INJECT_NON_SAFETY_KEYS: frozenset[str] = frozenset(
    {
        # Hardware-pinned controller tick (read-only in M1).
        "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS",
    }
)


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


class AppConfigSnapshot(BaseModel, frozen=True):
    """Full per-field snapshot returned by ``GET /api/config`` (D78).

    Sections mirror :class:`~roastpilot_agent.config.AppConfig`; each leaf
    value is a :class:`ConfigFieldMeta` carrying saved / effective / default /
    env_overridden / read_only.
    """

    controller: ControllerConfigSnapshot
    advisor: AdvisorConfigSnapshot
    safety: SafetyLimitsSnapshot


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


class AppConfigEdit(BaseModel):
    """The PUT /api/config request body — safety is intentionally excluded (D78).

    Only editable managed fields appear here. The server merges them into the
    saved-config file; the next roast picks them up.
    """

    controller: ControllerConfigEdit | None = None
    advisor: AdvisorConfigEdit | None = None


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

    Args:
        path: The path to the saved-config YAML file.
        data: The dict to serialise as YAML. Only editable managed fields
            should appear here — safety is excluded by the caller.

    Raises:
        OSError: If the file or its parent directories cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True)


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
    env_overridden = bool(env_var and _env_is_set(env_var) and env_var not in injected)
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
        late_maillard_trim_enabled=_meta(
            trim_saved.get("enabled"),
            trim.enabled,
            trim_def.enabled,
            None,  # nested — no single top-level env var
            description=(
                "Enable the anticipatory heat trim in the late-Maillard → FC window."
                " When off, the flat heat floor (100 %) is used to FC."
            ),
        ),
        late_maillard_trim_heat_percent=_meta(
            trim_saved.get("trim_heat_percent"),
            trim.trim_heat_percent,
            trim_def.trim_heat_percent,
            None,
            description=(
                "Trimmed heat level (%) held once the late-Maillard window opens."
                " Default 65 — a moderate reduction from 100 %, not a stall."
            ),
        ),
        late_maillard_trim_window_fc_eta_seconds=_meta(
            trim_saved.get("window_fc_eta_seconds"),
            trim.window_fc_eta_seconds,
            trim_def.window_fc_eta_seconds,
            None,
            description=(
                "Seconds before the predicted first crack at which the trim window"
                " opens. Default 60 s (late Maillard, ~1 min ahead of the crack)."
            ),
        ),
        late_maillard_trim_min_bean_temp_c=_meta(
            trim_saved.get("min_bean_temp_c"),
            trim.min_bean_temp_c,
            trim_def.min_bean_temp_c,
            None,
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
            None,
            description=(
                "Enable adaptive trim depth (#386). When enabled, the trim deepens"
                " on hotter approaches (high RoR, short FC-ETA). Default off."
            ),
        ),
        late_maillard_trim_base_trim=_meta(
            trim_saved.get("base_trim"),
            trim.base_trim,
            trim_def.base_trim,
            None,
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
            None,
            description=(
                "RoR sensitivity (°C/min per pp of trim deepening). Each extra"
                " °C/min above ror_ref deepens the cut by this many pp. Default 1.5."
            ),
        ),
        late_maillard_trim_k_eta=_meta(
            trim_saved.get("k_eta"),
            trim.k_eta,
            trim_def.k_eta,
            None,
            description=(
                "ETA sensitivity (s per pp of trim deepening). Each 1 s under"
                " eta_ref deepens the cut by this many pp. Default 0.2."
            ),
        ),
        late_maillard_trim_ror_ref=_meta(
            trim_saved.get("ror_ref"),
            trim.ror_ref,
            trim_def.ror_ref,
            None,
            description=(
                "RoR reference level (°C/min). Below this the RoR term contributes"
                " 0; above it the cut deepens. Default 8.0."
            ),
        ),
        late_maillard_trim_eta_ref=_meta(
            trim_saved.get("eta_ref"),
            trim.eta_ref,
            trim_def.eta_ref,
            None,
            description=(
                "ETA reference (seconds). The ETA term is 0 at the window boundary"
                " and deepens only when FC is closer than this. Default 60.0."
            ),
        ),
        late_maillard_trim_min_trim=_meta(
            trim_saved.get("min_trim"),
            trim.min_trim,
            trim_def.min_trim,
            None,
            description=(
                "Deepest permitted adaptive trim (%). The formula cannot go below"
                " this, preventing stall of first crack. Default 45."
            ),
        ),
        late_maillard_trim_max_trim=_meta(
            trim_saved.get("max_trim"),
            trim.max_trim,
            trim_def.max_trim,
            None,
            description=(
                "Shallowest permitted adaptive trim (%). The formula cannot go above"
                " this (adaptive depth is always a reduction). Default 75."
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

    return AppConfigSnapshot(
        controller=controller_snapshot,
        advisor=advisor_snapshot,
        safety=safety_snapshot,
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
    saved_raw = _load_saved_config(_config_file_path())
    injected_keys = _inject_saved_as_env(saved_raw)
    return AppConfig(), injected_keys


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
        _inject_section(cast("dict[str, Any]", section_val), prefix, injected)
    return frozenset(injected)


def _inject_section(
    section_dict: dict[str, Any],
    prefix: str,
    injected: set[str],
) -> None:
    """Recursively inject a section of the saved config as env vars.

    Args:
        section_dict: The section's value dict from the saved-config YAML.
        prefix: The env-var prefix accumulated so far (already uppercased).
        injected: Mutable set to record every env-var key that is written.
    """
    import json

    for key, val in section_dict.items():
        env_key = f"{prefix}{key.upper()}"
        if env_key in _NEVER_INJECT_NON_SAFETY_KEYS:
            # This non-safety field is read-only in the snapshot — skip it.
            continue
        if isinstance(val, dict):
            _inject_section(cast("dict[str, Any]", val), f"{env_key}__", injected)
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

    Args:
        edit: The validated edit from a ``PUT /api/config`` request.

    Raises:
        pydantic.ValidationError: If the merged config violates the schema.
        ConfigFileError: If the existing file is malformed (read phase).
        OSError: If the file cannot be written.
    """
    path = _config_file_path()
    existing = _load_saved_config(path)
    merged = apply_config_edit(edit, existing)
    _write_saved_config(path, merged)
