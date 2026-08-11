"""Shared Pydantic models and enums (component plan §4).

Scaffold stubs only — the full model set (including MCP state mirrors and
SSE event payloads) lands in E2. All temperatures are Celsius everywhere.

The shared enums here are plain ``Enum``, deliberately not ``StrEnum``:
comparing a member against a raw string must be a pyright strict error
(``reportUnnecessaryComparison``), per the AGENTS.md typed-vocabulary
invariant. Use ``.value`` at serialization boundaries.
"""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator


class RoastPhase(Enum):
    """Agent phases — the operator-facing truth (component plan §3).

    Lives here (not in controller.py) per D15: store, api, and advisor all
    consume the phase vocabulary, and importing it from controller.py would
    create import cycles once the tick loop wires those modules together.
    """

    IDLE = "idle"
    STARTING = "starting"
    PREHEATING = "preheating"
    ROASTING_PRE_FIRST_CRACK = "roasting_pre_first_crack"
    DEVELOPMENT = "development"
    COOLING = "cooling"
    COMPLETE = "complete"
    FAULTED = "faulted"
    OPERATOR_RECOVERY_REQUIRED = "operator_recovery_required"


ACTIVE_ROAST_PHASES: frozenset[RoastPhase] = frozenset(
    {
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
    }
)
"""Phases during which the machine may be hot with beans in play and
telemetry must be trustworthy. ``starting`` is excluded (the MCP session is
still being created — no telemetry exists yet); ``idle``/``complete``/
``faulted``/``operator_recovery_required`` are excluded (no active control).
"""


class RoastStyle(Enum):
    """Operator-facing roast-degree vocabulary (#405, D82).

    Plain ``Enum`` (D15): a string comparison against a member must stay a
    pyright strict error (``reportUnnecessaryComparison``), matching every
    other shared enum in this module (e.g. :class:`RoastPhase`,
    :class:`MicHealth`). Purely additive data modelling in this slice — it is
    not yet wired into any control law or the drop decision (Slice C of #405
    decides that precedence); see :data:`ROAST_STYLE_TARGETS` for the seeded
    corpus targets each style maps to.
    """

    LIGHT = "light"
    MEDIUM = "medium"
    DARK = "dark"


class RoastStyleTarget(BaseModel):
    """The canonical drop target for a :class:`RoastStyle` (#405, D82).

    A small Celsius/percent pair: the drop bean temperature and the
    development-time-ratio (DTR) target a style implies. These are corpus-seeded
    defaults (washed high-grown reference curves); a profile's own explicit
    ``target_drop_temp_c`` / ``target_development_percent`` may still override
    them per bean — Slice C of #405 wires the precedence into the deterministic
    drop control law. This model carries no control logic itself.
    """

    drop_temp_c: float = Field(gt=0)
    dtr_target: float = Field(gt=0, lt=100)


# Corpus-seeded (washed high-grown) drop targets per roast style (#405, D82).
# The 195-196 °C bitter cap holds regardless of style — that ceiling is
# enforced in safety.py (the pre-drop safety bound), not here; these are seed
# defaults a per-profile explicit target may still override (Slice C decides
# precedence when wiring the deterministic drop).
ROAST_STYLE_TARGETS: dict[RoastStyle, RoastStyleTarget] = {
    RoastStyle.LIGHT: RoastStyleTarget(drop_temp_c=188.0, dtr_target=15.0),
    RoastStyle.MEDIUM: RoastStyleTarget(drop_temp_c=193.0, dtr_target=18.0),
    RoastStyle.DARK: RoastStyleTarget(drop_temp_c=196.0, dtr_target=20.0),
}

#: The default roast style for profile creation / the UI (#405, D82). The
#: stored ``roast_style`` field on a profile itself defaults to ``None`` (not
#: this value) so every pre-#405 frozen ``profile_json`` / saved template still
#: round-trips unchanged; ``DEFAULT_ROAST_STYLE`` is only the UI/creation-time
#: seed for a NEW profile.
DEFAULT_ROAST_STYLE: RoastStyle = RoastStyle.MEDIUM


def roast_style_target(style: RoastStyle) -> RoastStyleTarget:
    """Look up the corpus-seeded drop target for a roast style.

    Args:
        style: The roast style to resolve.

    Returns:
        The :class:`RoastStyleTarget` (drop temperature + DTR) seeded for that
        style (#405, D82).
    """
    return ROAST_STYLE_TARGETS[style]


class RoastCommand(Enum):
    """MCP write commands the agent can issue (component plan §2 tool surface,
    writes only). The command×phase validity matrix in safety.py (E3-S5,
    D16) governs where each may execute."""

    START_ROAST_SESSION = "start_roast_session"
    SET_HEAT = "set_heat"
    SET_FAN = "set_fan"
    MARK_BEANS_ADDED = "mark_beans_added"
    MARK_FIRST_CRACK = "mark_first_crack"
    DROP_BEANS = "drop_beans"
    START_COOLING = "start_cooling"
    STOP_COOLING = "stop_cooling"
    EXPORT_ROAST_LOG = "export_roast_log"
    EMERGENCY_STOP = "emergency_stop"


class RoastEventKind(Enum):
    """Agent-level event kinds (component plan §5 ``roast_events.kind``).

    The persisted superset of MCP events; the SSE stream (plan §6) emits
    these plus the transport-only ``telemetry`` and ``heartbeat`` events,
    which have their own table/no persistence and are not event kinds here.
    """

    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    CHARGE_GUIDANCE = "charge_guidance"
    T0_DETECTED = "t0_detected"
    TURNING_POINT = "turning_point"
    DRYING_END = "drying_end"
    FIRST_CRACK = "first_crack"
    ADVISORY = "advisory"
    COMMAND_EXECUTED = "command_executed"
    COMMAND_FAILED = "command_failed"
    SAFETY_ALERT = "safety_alert"
    FAULT = "fault"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_ACKNOWLEDGED = "recovery_acknowledged"
    LOGS_EXPORTED = "logs_exported"
    RUN_COMPLETED = "run_completed"


class RoastEventSource(Enum):
    """Origin of an agent-level event (component plan §5 ``roast_events.source``)."""

    CONTROLLER = "controller"
    MCP = "mcp"
    OPERATOR = "operator"
    ADVISOR = "advisor"
    SAFETY = "safety"


class DropReason(Enum):
    """Which deterministic drop path fired (#405 Slice C/D88 amendment A1).

    Two policy-driven ``drop_beans`` paths share the same ``"policy"``
    command-trace source (D84's dev%/temp anchor and D88's decoupled ceiling
    guard — see ``CommandTraceSource`` in this module) — this typed value
    distinguishes WHICH one fired, carried as ``.value`` in the
    ``COMMAND_EXECUTED``/``COMMAND_FAILED`` event payload's ``reason`` key
    (observability only: no controller/safety code path branches on it, so it
    is never a string comparison in core logic, D15 — it exists purely so a
    trace reader, or a test, can tell the two apart without re-deriving it
    from bean temperature and dev% after the fact).
    """

    #: D84's deterministic anchor: ``bean_temp_c >= target_drop_temp_c`` AND
    #: ``dev_percent >= target_development_percent``, gated on the RoR-taper
    #: loop's own ``enabled``/``_post_fc_engaged`` bundle.
    DEVELOPMENT_TARGET = "development_target"
    #: D88 amendment A1's decoupled ceiling guard: ``bean_temp_c >=
    #: ceiling_guard_temp_c``, gated ONLY on its own
    #: ``ceiling_guard_drop_enabled`` flag + DEVELOPMENT phase — fires
    #: regardless of the RoR-taper loop's flag or engagement (the safety
    #: anchor every roast needs, not a taper feature).
    CEILING_GUARD = "ceiling_guard"


class PostFcHeatAuthorityState(Enum):
    """The post-FC RoR-taper loop's current heat-ceiling regime (D96, #559;
    PR #560 Codex finding — the diagnostics gap; moved here from
    ``post_fc_control.py`` in D96 slice 2, #559, so ``advisor.py``'s
    ``AdvisorContext`` can carry it without ``models.py`` importing
    ``post_fc_control.py`` — the SAME reasoning :class:`RoastPhase` and
    :class:`DropReason` above already document: the vocabulary lives where
    every consumer can reach it without a cycle, not where it is
    semantically "owned").

    A plain ``Enum`` (D15: never string-compared in core logic). Three
    values, covering every state
    ``post_fc_control.PostFcRorController.compute`` can be in:

    * ``HOLDING`` — the plain D88 never-add-heat-beyond-entry ceiling; no
      recovery has ever engaged this engagement, or it fully settled back to
      the D88 base after a prior exit.
    * ``RECOVERING`` — the D96 recovery ceiling is FULLY ACTIVE (raised to
      ``heat_engage_percent + recovery_headroom_percentage_points``,
      clamped to ``heat_ceiling_percent``).
    * ``GLIDING`` — recovery has EXITED but the effective ceiling has not
      yet fully descended back to the D88 base (the exit-glide tail, D96's
      addendum) — the state a bare ``recovery_active`` boolean could not
      distinguish from ``HOLDING`` (a Codex finding on PR #560: the boolean
      flips ``False`` the instant exit is confirmed even though the
      ceiling can still sit well above the D88 base for several more ticks,
      hiding the elevated-authority tail from any diagnostic or
      guard-eligibility check that reads only the boolean).

    ``post_fc_control.PostFcControlOutput.recovery_active`` is kept as a
    derived ``bool`` (``state is not HOLDING``) for the callers slice 1
    already shipped, but any NEW code reasoning about "is the ceiling still
    elevated above the D88 base" (e.g. a guard-eligibility check, or
    ``AdvisorContext.post_fc_heat_authority_state``, #559 slice 2) must read
    this field directly, never the derived boolean alone — ``RECOVERING``
    and ``GLIDING`` both need to be caught, and only this enum distinguishes
    them.
    """

    HOLDING = "holding"
    RECOVERING = "recovering"
    GLIDING = "gliding"


# --- #197: microphone / first-crack capture-alive health (observability) ---
#
# The MCP audio first-crack pipeline reports a rich liveness status
# (``mcp_client.FirstCrackStatus``); the agent projects a small, capture-alive
# slice of it onto the SSE telemetry frame and the run snapshot so the SPA can
# render a green/red/amber mic icon. This is pure observability — no safety
# logic, no controller-loop change, advisory-only. Per the Raspberry Pi
# performance constraint it carries ONLY the counters the MCP already computes;
# no RMS / per-window level work is done here (deferred follow-up, #33).

#: The MCP's first-crack runtime status, mirrored here as the SPA-facing wire
#: form (matches ``mcp_client.FirstCrackRuntimeStatus`` byte-for-byte). Spelled
#: as a ``Literal`` rather than imported from ``mcp_client``: that module imports
#: *this* one, so importing it back would cycle. A test pins the two in sync.
FirstCrackStatusLiteral = Literal[
    "disabled", "manual", "pending", "detected", "faulted", "unavailable"
]


class MicHealth(Enum):
    """Derived microphone / first-crack capture health the SPA icon maps to.

    Plain ``Enum`` (D15): a string comparison against a member is a pyright
    strict error. Observability-only — never a control or safety signal.

    The mapping from the MCP first-crack status (component plan §7 diagnostics):

    - ``OK`` (green) — audio capture is running and the detector is live:
      ``audio_running`` is true and FC status is ``pending`` or ``detected``.
    - ``ERROR`` (red) — the device won't open or the detector failed: FC status
      is ``faulted`` or ``unavailable``.
    - ``IDLE`` (amber/grey) — no active audio capture: FC mode is disabled or
      manual, or capture has not started yet (any other state).
    """

    OK = "ok"
    ERROR = "error"
    IDLE = "idle"


class MicStatus(BaseModel):
    """Capture-alive health of the microphone / first-crack audio pipeline (#197).

    A read-only projection of ``mcp_client.FirstCrackStatus`` onto the operator
    surface: the derived :class:`MicHealth` the icon renders, plus the raw
    capture-alive fields behind it for the tooltip. It carries only counters the
    MCP already computes (Pi performance: no per-window level work, #33).

    The configured microphone *device name* is deliberately absent: it is not on
    the MCP ``FirstCrackStatus`` (nor the runtime-config snapshot), so this
    contract does not promise it.
    """

    mic_health: MicHealth
    audio_running: bool
    fc_status: FirstCrackStatusLiteral
    queued_window_count: int
    emitted_window_count: int
    dropped_window_count: int
    processed_window_count: int
    reason: str | None = None
    # Overflow diagnostics (MCP 0.1.13, coffee-roaster-mcp#190, #539): capture-
    # side frame-loss visibility, mirroring `mcp_client.FirstCrackStatus`'s own
    # trio 1:1. Defaults keep a pre-0.1.13 MCP's payload valid (its
    # FirstCrackStatus mirror already defaults these to 0/0.0 — see #538).
    overflow_count_last_minute: int = 0
    estimated_lost_audio_ms_last_minute: float = 0.0
    total_overflow_count: int = 0

    @classmethod
    def from_first_crack_status(
        cls,
        *,
        status: FirstCrackStatusLiteral,
        audio_running: bool,
        queued_window_count: int,
        emitted_window_count: int,
        dropped_window_count: int,
        processed_window_count: int,
        reason: str | None = None,
        overflow_count_last_minute: int = 0,
        estimated_lost_audio_ms_last_minute: float = 0.0,
        total_overflow_count: int = 0,
    ) -> "MicStatus":
        """Project the MCP first-crack status fields into a :class:`MicStatus`.

        Takes the raw scalar fields (not the ``mcp_client.FirstCrackStatus``
        mirror itself) so this module stays free of an import cycle with
        ``mcp_client`` (which imports this module). The derived
        :class:`MicHealth` follows the mapping documented on that enum.

        Args:
            status: The MCP first-crack runtime status.
            audio_running: Whether the audio capture loop is alive.
            queued_window_count: Windows queued for inference.
            emitted_window_count: Windows emitted to the detector.
            dropped_window_count: Windows dropped (backpressure).
            processed_window_count: Windows the detector processed.
            reason: Optional MCP-supplied reason / last-error string.
            overflow_count_last_minute: Capture-buffer overflow events in the
                trailing minute (MCP 0.1.13, #539). Defaults to 0 for a
                pre-0.1.13 MCP.
            estimated_lost_audio_ms_last_minute: Estimated lost audio in the
                trailing minute, in milliseconds (MCP 0.1.13, #539).
            total_overflow_count: Cumulative overflow events for the whole
                capture session (MCP 0.1.13, #539).

        Returns:
            The projected capture-alive status with its derived health.
        """
        if status in ("faulted", "unavailable"):
            health = MicHealth.ERROR
        elif audio_running and status in ("pending", "detected"):
            health = MicHealth.OK
        else:
            health = MicHealth.IDLE
        return cls(
            mic_health=health,
            audio_running=audio_running,
            fc_status=status,
            queued_window_count=queued_window_count,
            emitted_window_count=emitted_window_count,
            dropped_window_count=dropped_window_count,
            processed_window_count=processed_window_count,
            reason=reason,
            overflow_count_last_minute=overflow_count_last_minute,
            estimated_lost_audio_ms_last_minute=estimated_lost_audio_ms_last_minute,
            total_overflow_count=total_overflow_count,
        )


class RoastTelemetry(BaseModel):
    """Minimal controller-facing telemetry reading (E4).

    E5's typed MCP mirrors construct this from ``RoastSessionState``; the
    controller's tick pipeline consumes it. Derived metrics (RoR) are
    passed through from MCP, never recomputed (plan §2).

    ``mic_status`` is the capture-alive projection (#197), carried here so it
    rides the same live/replay telemetry path as ``first_crack_detected``;
    ``None`` when the source state exposes no first-crack status (e.g. a flat
    replay export, whose ``last_state`` is ``None``).

    ``t0_backdate_seconds`` / ``first_crack_backdate_seconds`` carry the
    MCP-reported backdating *delta* (#337): the seconds between the MCP's
    confirmation tick and the backdated turning-point / crack-onset instant the
    v0.1.7 server reports (coffee-roaster-mcp#169/#170). Both deltas are computed
    *inside* the MCP monotonic domain (``confirmed_at_* − onset``), so they are
    deltas — never absolute MCP timestamps, which are not comparable to the
    agent's own ``time.monotonic`` clock. The controller subtracts the delta from
    its receive-tick clock to anchor the charge / development origin at the true
    onset. ``None`` when the source carries no backdated event (a manual mark, an
    older payload, or no such event yet) — the controller then stamps at
    receive-tick, the pre-0.1.7 behaviour.

    ``ambient_temp_c`` / ``ambient_humidity_pct`` / ``ambient_pressure_hpa``
    carry the LATEST ambient reading (#464, D86 — revises D85), mirroring
    ``mic_status``'s precedent exactly: a pure observability projection off the
    MCP's ``ambient_status`` (mode/status + the ~30 s-cached triad), refreshed
    every tick. ``None`` when the MCP ambient status is not ``"ok"``, when its
    ambient runtime is no longer running (#732 — a stopped runtime keeps
    reporting ``"ok"`` over a frozen reading), when any member of the reading is
    non-finite (#752 — the triad is voided as a unit; see
    :func:`~roastpilot_agent.mcp_client.project_live_ambient`), or when the
    source state carries no ambient status at all (an older MCP / replay
    export). Distinct from the
    ONE-TIME charge-instant value :meth:`RoastStore.set_ambient` persists onto
    ``roast_runs`` (#342, D85) — that capture path is untouched.

    ``ambient_age_seconds`` carries how long the current reading has been the
    current one (#732), measured in the **agent's** clock from its first
    observation of that reading — never by subtracting the MCP's own monotonic
    stamp, which is not comparable across processes. ``None`` means "no reading"
    or "age unknown", the fail-closed value. No safety gate or control path
    reads any of these fields; ambient reaches the *advisor* only through the
    #709 doctrine's own ``enabled`` gate, which additionally declines a reading
    older than its configured bound.
    """

    bean_temp_c: float
    env_temp_c: float
    age_seconds: float = 0.0
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None
    t0_detected: bool = False
    first_crack_detected: bool = False
    cooling_on: bool = False
    mic_status: MicStatus | None = None
    t0_backdate_seconds: float | None = None
    first_crack_backdate_seconds: float | None = None
    ambient_temp_c: float | None = None
    ambient_humidity_pct: float | None = None
    ambient_pressure_hpa: float | None = None
    ambient_age_seconds: float | None = None


class AppliedRoasterState(BaseModel):
    """Post-command roaster state actually applied by the driver (#507).

    Returned by :class:`~roastpilot_agent.controller.CommandExecutor`'s
    ``drop_beans`` and ``emergency_stop`` — both commands change heat/fan/
    cooling as a hardware side effect of the command itself, rather than
    through an explicit ``set_targets`` call. The controller adopts these
    fields into its own commanded-value mirrors (``_current_heat`` /
    ``_current_fan``) so a drop or an e-stop is reflected the same tick it
    lands, instead of leaving those mirrors (and everything downstream —
    ``telemetry_snapshots`` rows, the SSE telemetry frame, the dashboard
    readout) holding the last pre-command ``set_targets`` values for the rest
    of the run. Sourced from the MCP command result's own event payload
    (``beans_dropped`` / ``fault``), never a hardcoded driver constant — the
    driver, not the controller, owns what a drop or an e-stop actually sets.
    """

    heat_level_percent: int = Field(ge=0, le=100)
    fan_level_percent: int = Field(ge=0, le=100)
    cooling_on: bool


# Bean species (botanical) — a constrained ``Literal`` deliberately, NOT a
# ``models.py`` ``Enum``: an enum here would trip the safety-reviewer escalation
# (the rubric routes any ``models.py`` enum change through it) even though bean
# identity is not safety-bearing. A ``Literal`` keeps the change lead-verifiable
# and is equally cloud-friendly (D29): structured, not free text. Species is the
# botanical level (arabica/robusta/…) and is distinct from the cultivar carried
# by ``bean_varietal`` (Heirloom, Bourbon, SL28…).
BeanSpecies = Literal["arabica", "robusta", "liberica", "excelsa"]

# Post-harvest processing method (#291) — a constrained ``Literal``, deliberately
# NOT a ``models.py`` ``Enum`` (an enum here would trip the safety-reviewer
# escalation, which routes any ``models.py`` enum change through it, even though
# processing is not safety-bearing). The learning loop (D42) keys per-origin
# advisor levers on this axis, so it is structured rather than free text (and is
# distinct from the free-text process notes an operator may also put in
# ``description``). ``"other"`` is the explicit escape hatch for an uncommon
# process so the value stays a closed set.
ProcessingMethod = Literal["washed", "natural", "honey", "anaerobic", "wet_hulled", "other"]


class _BeanProfileFieldsBase(BaseModel):
    """Shared bean-identity + roast-target fields for the two profile models (#303).

    The single source of truth for every field that a reusable :class:`BeanProfile`
    template and an instantiated :class:`RoastProfile` have in common — bean
    identity (name, origin, varietal, country, farm, description, species, blend
    flag, #291 processing + altitude, #315 source URL), the charge guidance band, the initial
    heat/fan levers, and the drop/development targets — together with the
    whitespace-normalizing validators and the guidance-range check.

    Pulled out as a base so the two models cannot drift (#303): the only
    difference between them is the per-roast ``bean_weight_grams`` (on
    :class:`RoastProfile`) versus the template ``default_bean_weight_grams`` +
    identity fields (on :class:`BeanProfile`). Not used directly — it carries no
    weight field on its own — so it is an internal base, not part of the public
    API. All temperatures are Celsius.
    """

    name: str = Field(min_length=1)
    bean_origin: str = Field(min_length=1)
    bean_varietal: str | None = None
    country: str | None = None
    """Producing country (e.g. Ethiopia, Colombia, Brazil). Optional for
    back-compat; for a blend this is the primary bean's country."""
    farm: str | None = None
    """The specific farm / co-op / washing station / region (e.g. "Gedeb —
    Worka Sakaro", "Finca El Injerto"). Optional for back-compat."""
    description: str | None = None
    """Free text: process (washed/natural/honey), tasting notes, lot, and — for
    a blend — the secondary beans / components. Optional for back-compat."""
    bean_species: BeanSpecies | None = None
    """Botanical species (arabica/robusta/liberica/excelsa) — distinct from the
    cultivar in ``bean_varietal``. A constrained ``Literal``, not an ``Enum``
    (see ``BeanSpecies``). Optional for back-compat."""
    is_blend: bool = False
    """Whether the drum held a blend. When true, the structured fields describe
    the primary bean and the secondaries are recorded in ``description``."""
    processing: ProcessingMethod | None = None
    """Post-harvest processing method (#291): washed / natural / honey /
    anaerobic / wet_hulled / other. A constrained ``Literal``, not an ``Enum``
    (see ``ProcessingMethod``). Optional for back-compat; one of the per-origin
    axes the learning loop (D42) keys advisor levers on. Distinct from any
    free-text process notes in ``description``."""
    altitude_m: int | None = Field(default=None, ge=0, le=4000)
    """Growing altitude in metres above sea level (#291). Optional for
    back-compat; bounded to a sane coffee-growing range (0–4000 m). Another
    per-origin learning-loop (D42) axis. For a blend this is the primary bean's
    altitude."""
    source_url: str | None = None
    """Product / source URL for the bean (#315): the page it was bought from
    (e.g. the roaster's product listing). Optional for back-compat; a blank /
    whitespace-only value normalizes to ``None`` like the other optional
    identity fields. Validated as a well-formed ``http(s)`` URL — lenient
    operator metadata, but a non-empty value must at least be a parseable
    http(s) link so the UI never renders a broken anchor. Carried into the
    corpus for provenance / re-ordering (D42)."""
    charge_guidance_min_c: float = 170.0
    # The guidance ceiling deliberately equals the pre-T0 safety bound
    # (config.SafetyLimits.pre_t0_max_bean_temp_c, default 200.0): operators
    # are guided to charge before the hard bound trips. A scaffold test pins
    # charge_guidance_max_c <= pre_t0_max_bean_temp_c; keep them in sync.
    charge_guidance_max_c: float = 200.0
    initial_heat_percent: int = Field(ge=0, le=100)
    initial_fan_percent: int = Field(ge=0, le=100)
    pre_fc_heat: int | None = Field(default=None, ge=10, le=100)
    """The per-bean deterministic pre-first-crack HEAT target the controller
    drives every tick pre-FC (D59 / #318, option C; refines D35). When set, it
    replaces the global :class:`~roastpilot_agent.config.PreFirstCrackLevers`
    ``heat_target_percent`` for this bean; when ``None`` the controller falls back
    to that config default (100 %). Distinct from ``initial_heat_percent``, which
    only SEEDS the ``start_run`` command and is then overwritten by the
    deterministic policy — ``pre_fc_heat`` is the value the policy actually holds
    to first crack. Optional / defaulted ``None`` for back-compat so every frozen
    ``profile_json`` and saved template from before #318 still deserializes.
    Bounded ``ge=10`` (not 0) to match
    :attr:`~roastpilot_agent.config.LateMaillardTrim.trim_heat_percent` and the
    "no near-zero heat during active roasting" invariant: a typo'd near-zero
    pre-FC heat would stall the roast, so it is rejected at construction. The
    resolved value stays bounded by the pre-FC safety box (the policy clamps it
    in range; the #327 trim still composes ≤ the resolved floor)."""
    pre_fc_fan: int | None = Field(default=None, ge=0, le=100)
    """The per-bean deterministic pre-first-crack FAN target the controller drives
    every tick pre-FC (D59 / #318, option C; refines D35). When set, it replaces
    the global :class:`~roastpilot_agent.config.PreFirstCrackLevers`
    ``fan_target_percent`` for this bean (e.g. a delicate natural at fan 20); when
    ``None`` the controller falls back to that config default (30 %). Distinct from
    ``initial_fan_percent``, which only SEEDS the ``start_run`` command and is then
    overwritten by the deterministic policy. Optional / defaulted ``None`` for
    back-compat. The resolved value stays bounded by the pre-FC safety box: a
    value above the configured ``fan_ceiling_percent`` is CLAMPED to the ceiling by
    the policy (never honoured blindly — the every-write-through-safety
    invariant), so the field is bounded to 0–100 here but the runtime ceiling is
    enforced where the config-side ceiling is known (the policy), not on the
    config-blind profile model."""
    target_drop_temp_c: float = Field(gt=0)
    target_development_percent: float = Field(gt=0, lt=100)
    roast_style: RoastStyle | None = None
    """The roast-style dimension (#405, D82): light / medium / dark. Optional /
    defaulted ``None`` for back-compat so every frozen ``profile_json`` and
    saved template from before #405 still deserializes unchanged. When ``None``
    the profile's explicit ``target_drop_temp_c`` / ``target_development_percent``
    remain authoritative — Slice C of #405 decides the precedence once a style
    is wired into the deterministic drop control law. The creation/UI default
    for a NEW profile is :data:`DEFAULT_ROAST_STYLE` (``MEDIUM``), but the stored
    field itself defaults ``None`` so old profiles round-trip without it."""

    @field_validator("name", "bean_origin", "bean_varietal")
    @classmethod
    def _strip_and_require_content(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace; whitespace-only strings are invalid."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("country", "farm", "description")
    @classmethod
    def _strip_optional_identity(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace on the optional identity fields.

        Unlike the required fields, an empty / whitespace-only value normalizes
        to ``None`` rather than raising: these are optional metadata an operator
        may leave blank, and a blank field is simply "unset", not invalid.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("source_url")
    @classmethod
    def _strip_and_check_source_url(cls, value: str | None) -> str | None:
        """Normalize blank to ``None``; require a well-formed http(s) URL otherwise.

        Lenient operator metadata: an empty / whitespace-only value is "unset"
        (``None``), not an error. A non-empty value must parse as an absolute
        ``http``/``https`` URL with a host — so the UI never renders a broken
        anchor and the corpus link is dereferenceable. Validated with the
        stdlib ``urlsplit`` (no extra dependency, no ``HttpUrl`` object that
        would change the serialized JSON shape and complicate the frozen
        ``profile_json`` round-trip).

        Beyond scheme + host, the value is rejected if it carries userinfo
        (``user:pass@host`` — a credential that must never be persisted in the
        corpus or rendered into an anchor) or a malformed port (which would make
        the anchor broken). ``urlsplit`` parses a bad port lazily, so a port is
        validated by accessing :attr:`~urllib.parse.SplitResult.port` (it raises
        ``ValueError`` on a non-numeric / out-of-range port).
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        parsed = urlsplit(stripped)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("must be a well-formed http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("must not embed credentials (userinfo)")
        try:
            parsed.port  # noqa: B018 — accessing .port validates it (raises on a bad port)
        except ValueError as exc:
            raise ValueError("must not have a malformed port") from exc
        return stripped

    @model_validator(mode="after")
    def _check_guidance_range(self) -> "_BeanProfileFieldsBase":
        """The charge guidance band must be a non-empty range."""
        if self.charge_guidance_min_c >= self.charge_guidance_max_c:
            raise ValueError(
                "charge_guidance_min_c must be below charge_guidance_max_c "
                f"({self.charge_guidance_min_c} >= {self.charge_guidance_max_c})"
            )
        return self


class RoastProfile(_BeanProfileFieldsBase):
    """Minimal static roast profile (decision D7) with richer bean identity (#164).

    No curve targets in M1: name, bean identity, charge guidance range,
    initial heat/fan, target drop temperature, target development percent.
    The profile is frozen into ``roast_runs.profile_json`` at run start
    (plan §5); hardware safety limits live in config, not here.

    Bean identity (#164) records what was actually in the drum. Beyond the
    original flat ``bean_origin`` + optional ``bean_varietal`` (cultivar), it
    captures the producing ``country``, the specific ``farm`` / co-op / washing
    station / region, the botanical ``bean_species``, an ``is_blend`` flag, and a
    free-text ``description`` (process, tasting notes, lot, and — for a blend —
    the secondary beans). The blend model is deliberately simple: the *primary*
    bean carries the structured fields and the secondaries live in
    ``description`` — a fully structured component list is out of scope (#164).

    The #291 additions extend that identity with the per-origin axes the learning
    loop (D42) keys on: the post-harvest ``processing`` method and growing
    ``altitude_m``.

    The bean-identity + target fields live on the shared :class:`_BeanProfileFieldsBase`
    so this model and the reusable :class:`BeanProfile` template (#303) cannot
    drift; the only field this model adds is the per-roast ``bean_weight_grams``.

    Backward compatibility: every #164 and #291 field is optional / defaulted so a
    frozen ``roast_runs.profile_json`` from before either change (the pre-#164
    shape carried only ``bean_origin`` + ``bean_varietal``) still deserializes
    unchanged.
    """

    bean_weight_grams: float = Field(gt=0)


def recording_origin_slug(profile: RoastProfile) -> str | None:
    """Derive a recording-origin slug from a roast profile (v0.1.9, #176).

    Joins the populated ``country`` / ``bean_origin`` / ``name`` fields into a
    lowercase hyphen slug (e.g. ``"colombia-excelso-huila-washed"``) so the MCP
    export filename carries a human-readable origin. The MCP re-slugifies, so this
    only needs to surface the identity words; punctuation and spacing are
    normalised to single hyphens.

    Those three fields routinely overlap (the Colombia seed has country ==
    bean_origin == ``"Colombia"`` and a ``"Colombia ..."`` name), so repeated
    words are deduped, first-seen order preserved. If no field yields any slug
    characters (all empty / punctuation-only), returns ``None`` so the caller
    skips the metadata call and the MCP falls back safely.

    Lives in ``models`` (with :class:`RoastProfile`) so the per-origin
    recording-count query in :mod:`store` (#385) can derive the same slug from a
    completed run's frozen ``profile_json`` without importing the controller.

    Args:
        profile: The active roast profile.

    Returns:
        A hyphen-slug like ``"colombia-excelso-huila-washed"``, or ``None`` when
        no usable identity text is available.
    """
    parts = [profile.country, profile.bean_origin, profile.name]
    raw = " ".join(part for part in parts if part)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    # country / bean_origin / name routinely overlap (the Colombia seed has
    # country "Colombia", bean_origin "Colombia", name "Colombia Excelso Huila
    # (Washed)" → "colombia-colombia-colombia-..."), so dedupe repeated words,
    # preserving first-seen order, for a clean origin like "colombia-excelso-huila-washed".
    seen: set[str] = set()
    deduped: list[str] = []
    for word in slug.split("-"):
        if word and word not in seen:
            seen.add(word)
            deduped.append(word)
    return "-".join(deduped) or None


class BeanProfile(_BeanProfileFieldsBase):
    """A reusable, saved bean-profile template the operator picks per roast (#303).

    The saved library entry behind the Start-Roast dropdown (D45, amends D29/D7):
    it carries every :class:`RoastProfile` bean-identity + roast-target field
    EXCEPT the per-roast charge weight, and adds a stable ``id``, the
    ``created_at`` / ``updated_at`` timestamps, and a ``default_bean_weight_grams``
    that pre-fills (but does not fix) each roast's charge weight.

    A roast does not start from a ``BeanProfile`` directly: :meth:`to_roast_profile`
    instantiates a frozen :class:`RoastProfile` from the template + the entered
    charge weight, and the start-roast path / ``roast_runs.profile_json`` snapshot
    are unchanged — so editing a saved profile only affects FUTURE roasts; past
    roasts keep their frozen snapshot (the #303 edit-is-safe-by-construction
    guarantee). All temperatures are Celsius.
    """

    id: str = Field(min_length=1)
    created_at: str
    """ISO-8601 UTC instant the profile was first saved."""
    updated_at: str
    """ISO-8601 UTC instant of the most recent edit (equals ``created_at`` when
    never edited)."""
    default_bean_weight_grams: float = Field(gt=0)
    """The charge weight (grams) that pre-fills a new roast's form; the operator
    may adjust it per roast (you may roast 200 g or 250 g of the same bean)."""

    def to_roast_profile(self, bean_weight_grams: float) -> RoastProfile:
        """Instantiate a :class:`RoastProfile` from this template + a charge weight.

        Builds the frozen-at-run-start profile a roast actually uses: every shared
        bean-identity + target field copied verbatim, with the per-roast
        ``bean_weight_grams`` supplied by the caller (defaulted from
        ``default_bean_weight_grams`` at the UI, adjustable per roast). The
        template's ``id`` / timestamps are deliberately dropped — they are library
        bookkeeping, not part of the per-roast profile snapshot, so the
        ``roast_runs.profile_json`` shape and corpus integrity are unchanged
        (#303).

        Args:
            bean_weight_grams: The charge weight for this roast, in grams (> 0).

        Returns:
            A :class:`RoastProfile` ready for the start-roast path.
        """
        shared = self.model_dump(
            exclude={"id", "created_at", "updated_at", "default_bean_weight_grams"}
        )
        return RoastProfile(**shared, bean_weight_grams=bean_weight_grams)


# --- #303: bean-profile library CRUD wire models (D45) ---
#
# The Start-Roast dropdown's saved-profile contract. ``BeanProfileInput`` is the
# create/update request body — every reusable field the operator fills, WITHOUT
# the server-owned ``id`` / ``created_at`` / ``updated_at`` (the store stamps
# those). ``BeanProfileList`` is the GET envelope. The full :class:`BeanProfile`
# (with id + timestamps) is the create/update/get response. Celsius throughout,
# consistent with the rest of the API.


class BeanProfileInput(_BeanProfileFieldsBase):
    """``POST``/``PUT /api/bean-profiles`` request body (#303).

    Carries every saved bean-profile field the operator supplies — the shared
    bean-identity + roast-target fields plus ``default_bean_weight_grams`` — but
    NOT the server-owned ``id`` / ``created_at`` / ``updated_at``: the store mints
    the id and stamps the timestamps (create stamps both; update bumps
    ``updated_at`` only). The same body shape serves create and edit.
    """

    default_bean_weight_grams: float = Field(gt=0)
    """The charge weight (grams) that pre-fills a new roast's form; adjustable
    per roast."""


class BeanProfileList(BaseModel):
    """``GET /api/bean-profiles`` envelope (#303): the saved library, the
    dropdown renders from."""

    profiles: list[BeanProfile]


# --- #573 phase 1: add-bean-from-URL draft (add-bean-profile skill, productised) ---
#
# ``POST /api/beans/draft-from-url`` response contract. This is DELIBERATELY
# never persisted as a saved profile automatically: the endpoint fetches a
# vendor product page, extracts a bean identity, and drafts conservative
# first-roast targets for operator review. Runtime telemetry retains only a
# sanitized field-value baseline (no URL/evidence/prose) with a 24-hour claim
# deadline, clearing it on claim or orderly shutdown, or at the deadline
# (including after restart following an abrupt stop). Saving still uses the explicit
# ``POST /api/bean-profiles`` (``BeanProfileInput``) action. Human-in-the-loop
# by construction: there is no code path from a fetched URL to a saved
# profile that does not pass back through the operator.

BeanFieldSource = Literal["on_page", "origin_estimated"]
"""Per-field provenance for a :class:`BeanProfileDraft` (#573): ``"on_page"``
when the value was read from the vendor page text, ``"origin_estimated"``
when it was imputed — a conservative first-roast target, or a value the page
did not state. A constrained ``Literal``, deliberately not a ``models.py``
``Enum`` — matching the :data:`BeanSpecies` / :data:`ProcessingMethod`
precedent: this is bean metadata, not a safety verdict, so it stays OUT of
the enum surface the safety-reviewer escalation routes on."""


UNTRUSTED_TEXT_BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
UNTRUSTED_URL_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x20\\\x7f]")


class BeanProfileDraft(_BeanProfileFieldsBase):
    """A drafted, NOT-YET-SAVED bean profile from add-bean-from-URL (#573 phase 1).

    Returned by ``POST /api/beans/draft-from-url`` for the operator to
    review, edit, and save — this type is read-only advisory output. Neither
    it as a saved profile automatically. Runtime telemetry retains a sanitized
    field-value baseline (excluding URL, evidence, and prose) with a 24-hour
    claim deadline, clearing it on claim or orderly shutdown, or at the deadline
    (including after restart following an abrupt stop); saving remains
    ``POST /api/bean-profiles``
    (:class:`BeanProfileInput`) action, unchanged, so a saved profile is
    always the result of an explicit operator action, never an automatic
    side effect of drafting one.

    Carries every :class:`BeanProfile` field except the server-owned
    ``id``/timestamps (the same shape as :class:`BeanProfileInput`), plus:

    - :attr:`field_sources` — honest per-field provenance (see
      :data:`BeanFieldSource`): every bean-identity field the vendor page
      actually stated is ``"on_page"``; every roast TARGET (charge guidance,
      initial heat/fan, drop temperature, development percent, default
      weight) is always ``"origin_estimated"`` — a vendor page never states a
      development-percent target or a drop temperature, so these are never
      presented as scraped fact.
    - :attr:`field_evidence` — the model-cited verbatim quote backing each
      typed field's value (#627), for operator judgement now that the
      typed-field citation gates are permanently parked.
    - :attr:`scouting_note` — the conservative "scouting run" framing the
      operator sees alongside the drafted targets: a wrong target on an
      unfamiliar bean must not burn a batch.

    All temperatures Celsius, per the shared base.
    """

    default_bean_weight_grams: float = Field(gt=0)
    """The charge weight (grams) that would pre-fill a new roast's form if
    this draft is saved; adjustable per roast like every other profile."""

    @field_validator(
        "name",
        "bean_origin",
        "bean_varietal",
        "country",
        "farm",
        "description",
        "source_url",
        mode="before",
    )
    @classmethod
    def _strip_bidi_controls(cls, value: object) -> object:
        """Remove non-content bidi controls from untrusted drafted identity text."""
        if isinstance(value, str):
            return UNTRUSTED_TEXT_BIDI_CONTROLS.sub("", value)
        return value

    draft_attempt_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    """Opaque, one-use id joining this draft to an explicit later save.

    The extraction helper leaves it unset; the API service adds it only after
    durably recording a successful attempt. It never authorizes an automatic
    save.
    """

    is_blend: bool | None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    """Overrides the shared base's plain ``bool`` (#587): a DRAFT's blend
    flag is honestly tri-state, because "the page never mentioned blending"
    and "the page said single-origin" are different facts. ``True``/``False``
    when the vendor page explicitly addressed it (tracked as ``"on_page"`` in
    :attr:`field_sources`); ``None`` when the page said nothing at all (no
    ``field_sources`` entry — absent means unset, per the same convention
    every other field_sources-tracked field follows). Every OTHER profile
    type (:class:`BeanProfile`, :class:`RoastProfile`, ``BeanProfileInput``)
    keeps the base's plain ``bool``: the operator always resolves it to a
    concrete choice before saving/starting a roast, so tri-state has no
    meaning there — this override is scoped to the draft only."""

    field_sources: dict[str, BeanFieldSource] = Field(default_factory=dict)
    """Per-field provenance keyed by field name (e.g. ``"bean_varietal"``,
    ``"target_development_percent"``) — see :data:`BeanFieldSource`. A field
    absent from this map means it was left ``None``/unset (never found on the
    page and not imputed), not "on the label"."""

    field_evidence: dict[str, str] = Field(default_factory=dict)
    """Model-cited verbatim vendor-page quotes for the four TYPED fields only
    (``altitude_m``, ``processing``, ``bean_species``, ``is_blend``), keyed
    the same way as :attr:`field_sources` (#627). Every automated lexical
    citation gate for these four fields is now permanently parked (see
    :func:`~roastpilot_agent.bean_sourcing._draft_from_identity`'s
    docstring) — none of them promotes a field to ``"on_page"`` any more —
    so this map exists to surface the quote the model actually cited for
    OPERATOR judgement instead (#590's ledger), not to certify anything
    automatically. Entries are authenticity-checked (#633): the quote text
    is verified to appear verbatim (normalized, whole-phrase, within a
    single contiguous segment) on the fetched page before it is included —
    this authenticates the QUOTE'S EXISTENCE only, not the value-claim
    (certification gates parked per #590); a quote that fails this check
    (fabricated, or spliced across separate parts of the page) is dropped
    entirely rather than surfaced as a possible fabrication. An entry is
    present only when a quote was both captured AND authenticated for that
    field; a field with no captured/authenticated quote (or whose value was
    ``None``) is simply absent, the same "absent means unset" convention as
    ``field_sources``. Values are UNTRUSTED vendor page text passed straight
    through from the extraction — never render them unescaped — and are
    already bounded to 500 characters each at the extraction schema
    (``_ExtractedBeanIdentity``'s ``*_evidence`` fields); not re-validated
    here."""

    scouting_note: str = Field(min_length=1)
    """Operator-facing conservative-target framing text (#573): explains why
    the drafted targets are a de-risked first-roast starting point, not a
    fixed recipe."""


CatalogueReasonCode = Literal[
    "missing_country",
    "missing_processing",
    "novel_country_processing",
    "rated_pair_affinity",
]
"""Deterministic local reason codes for a catalogue recommendation (D121)."""


class CatalogueRecommendation(BaseModel):
    """One read-only, explainable product recommendation from a catalogue."""

    candidate_id: str = Field(pattern=r"^candidate-[0-9]{2}$")
    product_url: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=500)
    country: str | None = Field(default=None, max_length=500)
    processing: ProcessingMethod | None = None
    score: int = Field(ge=0)
    reason_codes: list[CatalogueReasonCode] = Field(max_length=5)
    reasons: list[Annotated[str, Field(max_length=600)]] = Field(max_length=5)

    @field_validator("product_url", mode="before")
    @classmethod
    def _reject_bidi_controls_in_product_url(cls, value: object) -> object:
        """Reject display-reordering and browser-ambiguous URL characters."""
        if isinstance(value, str) and (
            UNTRUSTED_TEXT_BIDI_CONTROLS.search(value)
            or UNTRUSTED_URL_UNSAFE_CHARACTERS.search(value)
        ):
            raise ValueError("product URL contains unsafe display characters")
        return value

    @field_validator("name", "country", "reasons", mode="before")
    @classmethod
    def _strip_bidi_controls(cls, value: object) -> object:
        """Remove display-reordering controls from untrusted recommendation text."""
        if isinstance(value, str):
            return UNTRUSTED_TEXT_BIDI_CONTROLS.sub("", value)
        if isinstance(value, list):
            return [
                UNTRUSTED_TEXT_BIDI_CONTROLS.sub("", item) if isinstance(item, str) else item
                for item in cast(list[object], value)
            ]
        return value


class CatalogueRecommendationList(BaseModel):
    """Bounded recommendations returned for one fetch-fresh catalogue page."""

    recommendations: list[CatalogueRecommendation] = Field(max_length=3)
    discovered_count: int = Field(ge=0, le=24)
    extracted_count: int = Field(ge=0, le=12)

    @model_validator(mode="after")
    def _check_extracted_subset(self) -> "CatalogueRecommendationList":
        """Extracted candidates cannot outnumber server discovery candidates."""
        if self.extracted_count > self.discovered_count:
            raise ValueError("extracted_count cannot exceed discovered_count")
        return self


# --- #567 Slice A: reference-curve retrieval + representation models ---
#
# A completed, well-rated past roast of THIS SAME bean, retrieved by
# `store.RoastStore.find_reference_run` / `load_reference_roast` for future
# advisor context. This slice is deliberately INERT: these models are read-only
# retrieval output, not wired into `AdvisorContext`, `start_roast`, the
# controller, replay, or config — that plumbing (and the AdvisorContext-facing
# `ReferenceCurveSample`/`ReferenceRoastLandmarks` shape sketched in the design
# note's §3.2, which carries actuated heat/fan levers these do not) is Slice B.
# These three models are the simpler store-level representation the retrieval
# logic itself produces; Slice B may adapt/project them onto the richer
# AdvisorContext shape rather than reusing them verbatim. All temperatures are
# Celsius.


class ReferenceCurveSample(BaseModel):
    """One downsampled point on a completed reference roast's telemetry curve.

    Aligned on charge-elapsed seconds (``t_s``) — the same clock origin the
    live curve window and the advisor's DTR clock already use (design note
    §3.1), so a live trajectory can overlay a reference one with no
    re-origining.
    """

    t_s: float
    """Seconds since charge (T0) at this sample."""
    bean_c: float
    """Bean temperature at this sample."""
    env_c: float | None = None
    """Environment (drum) temperature at this sample, when recorded."""
    ror_c_min: float | None = None
    """Bean rate-of-rise in °C/min at this sample, when recorded."""


class ReferenceLandmarks(BaseModel):
    """First-crack and drop landmarks for a completed reference roast.

    Extracted with the clock-safe, telemetry-phase-only rule pinned by the
    #567 design note §6.4a: first crack is the FIRST ``telemetry_snapshots``
    row tagged ``agent_phase == 'development'`` (development begins at FC),
    and drop is the LAST such row — never the run's final row, which can fall
    in the post-drop cooling tail (bean temperature keeps being recorded
    while it falls after drop). This rule stays entirely within the
    run-relative telemetry clock and never rebases against
    ``roast_events.monotonic_seconds``, a different clock origin (see the
    ``store-telemetry-event-clock-mismatch`` precedent).
    """

    first_crack_temp_c: float | None
    """Bean temperature at the first ``development``-phase telemetry row, or
    ``None`` if that row's temperature was never recorded."""
    first_crack_elapsed_s: float | None
    """Charge-elapsed seconds at the first ``development``-phase telemetry
    row, or ``None`` if that row's charge-elapsed clock was never recorded."""
    drop_temp_c: float | None
    """Bean temperature at the last ``development``-phase telemetry row, or
    ``None`` if that row's temperature was never recorded."""
    drop_development_percent: float | None
    """Development-time-ratio percent at the last ``development``-phase
    telemetry row — the controller's own real-time DTR reading, read directly
    off the stored column rather than reconstructed from an event timestamp
    (design note §4's DTR-provenance note) — or ``None`` if that row never
    recorded one."""
    operator_rating: int
    """The reference run's 1-5 star operator rating."""


class ReferenceRoast(BaseModel):
    """A completed, well-rated past roast of the same bean (#567 Slice A).

    Pure retrieval + representation data returned by
    :meth:`~roastpilot_agent.store.RoastStore.find_reference_run` /
    :meth:`~roastpilot_agent.store.RoastStore.load_reference_roast`. Carries
    no control authority and is not (yet) read by the controller, safety, the
    advisor, or the API — Slice B wires it into ``AdvisorContext``.
    """

    source_run_id: str
    """The retrieved run's ``roast_runs.id``."""
    origin_slug: str
    """The :func:`recording_origin_slug` both this roast and the reference
    roast share."""
    landmarks: ReferenceLandmarks
    curve: list[ReferenceCurveSample]
    """At most 30 downsampled telemetry points, always including the first
    and last usable row (design note §3.1)."""


# --- E7-S1: REST API response models (component plan §6) ---
#
# Typed response models for the REST surface live here, the shared-models
# home. The decision-trace verdict/status fields below are spelled as
# ``Literal`` wire forms rather than imported enums: ``safety.SafetyVerdict``
# and ``advisor.RoastDecision`` depend on this module, so importing them back
# would cycle. The literals mirror the store CHECK constraints (plan §5)
# exactly — a drift would fail the timeline read, not pass silently.


class MCPChildStatus(Enum):
    """coffee-roaster-mcp child-process liveness for ``GET /api/health``.

    Plain ``Enum`` (D15): a string comparison against a member is a pyright
    strict error. ``not_configured`` is the API-only mode where no MCP child
    is wired yet (E7 ships the contract; E9 wires the live child)."""

    RUNNING = "running"
    STOPPED = "stopped"
    NOT_CONFIGURED = "not_configured"


class AdvisorHealthStatus(Enum):
    """Advisor reachability state for the startup readout + ``/api/health``.

    Plain ``Enum`` (D15): a string comparison against a member is a pyright
    strict error. The advisor is advisory-only, so ``UNREACHABLE`` is an
    observability signal, never a serve blocker.
    """

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"


class AdvisorHealth(BaseModel):
    """Advisor reachability probe result (issue #168).

    Carried in the startup readout and exposed on ``GET /api/health`` so the
    operator learns the advisor is dead *before* committing a real roast,
    rather than after (the #134 expired-key failure: ``advisor configured``
    was a comforting half-truth). The advisor is advisory-only, so this is
    pure observability — an unreachable advisor never blocks serve.

    States:

    - ``REACHABLE`` — a cheap probe completion returned; the configured
      provider + model answered.
    - ``UNREACHABLE`` — the probe failed (auth 401/402, model 404, transport,
      or timeout); ``error`` carries the provider message.
    - ``NOT_CONFIGURED`` — no advisor is wired (advisory-paused mode); the
      controller runs deterministically without advice.
    """

    status: AdvisorHealthStatus
    provider: str | None = None
    model_slug: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """``GET /api/health``: liveness + MCP child status + active run id.

    ``advisor`` carries the most recent advisor reachability probe (issue
    #168) so the dashboard can render an ADVISOR-OFFLINE state; it is ``None``
    when no probe has run (e.g. the E7 API-only contract path).

    ``instance_id`` is a ``uuid4`` minted ONCE per server process (#516,
    follow-up to the #513 port-impostor incident — docs/recent-fixes.md, 12
    Jul) — never persisted, never derived from anything durable. A second
    process wildcard-bound to the same port (SO_REUSEADDR) answers with a
    DIFFERENT ``instance_id`` even though every other field can look
    identical (same version, ``active_run_id: null``). The UI cannot see
    which process answered a socket, but it CAN notice this identity change:
    the start-roast confirm path compares the id observed with the 201
    against a subsequent fresh health read and surfaces "answers are coming
    from a different server process" distinctly from a generic failure.
    Passive health consumers (nav chip, dashboard) must NOT compare it —
    only the confirm-loop's cross-request check does, otherwise a normal
    server restart would false-alarm every passive consumer.
    """

    status: Literal["ok"] = "ok"
    version: str
    instance_id: str
    mcp_child: MCPChildStatus
    mcp_hardware_clear_required: bool = False
    """Whether an unconfirmed child teardown blocks a fresh MCP spawn (#668).

    Read-only process state for the operator UI. ``True`` never proves the
    hardware is safe; it means the operator must verify it physically before
    using the explicit acknowledgement endpoint.
    """
    mcp_teardown_incident_id: str | None = None
    """Opaque ID the explicit acknowledgement must echo for this incident."""
    active_run_id: str | None = None
    advisor: AdvisorHealth | None = None


class LogManifest(BaseModel):
    """Export manifest (``mcp_client.ExportRoastLogResult`` shape, persisted
    in ``roast_runs.export_manifest_json``). Extra fields (e.g. session id)
    are ignored when validating the stored payload."""

    log_dir: str
    jsonl_path: str
    csv_path: str
    summary_path: str
    ready: bool
    note: str | None = None


class RoastSummary(BaseModel):
    """History list item (plan §6: id, started, outcome, bean, rating, dev %).

    The richer bean-identity fields (#164: ``country``, ``bean_species``,
    ``is_blend``; #291: ``processing``, ``altitude_m``) are projected from the
    frozen profile so the history list can show producing country, processing,
    altitude, and a blend marker without opening each run. They are optional /
    defaulted for back-compat with pre-#164 / pre-#291 frozen profiles.
    """

    id: str
    started_at_utc: str
    completed_at_utc: str | None = None
    first_crack_at_utc: str | None = None
    """UTC ISO-8601 wall-clock time of this run's first-crack event (#111),
    projected from the earliest persisted ``first_crack`` roast event. ``None``
    when no first crack was detected or operator-marked (back-compat: a pre-FC
    run, or any run that never reached first crack). The history list renders it
    as the FC-time column; the field is advisory display only, never control."""
    agent_phase: RoastPhase
    outcome: Literal["completed", "aborted", "faulted"] | None = None
    bean_origin: str
    bean_varietal: str | None = None
    country: str | None = None
    bean_species: BeanSpecies | None = None
    is_blend: bool = False
    processing: ProcessingMethod | None = None
    altitude_m: int | None = None
    rating: int | None = None
    roasted_weight_grams: float | None = None
    """Operator-entered roasted-out weight in grams (#388), or ``None`` when not
    yet weighed. The green/charge weight lives on the frozen profile
    (``bean_weight_grams``)."""
    corrected_charge_grams: float | None = None
    """Operator-entered CORRECTED charge/green weight in grams (#520), or
    ``None`` when never corrected. The frozen ``profile.bean_weight_grams``
    stays what the controller/advisor actually ran with (never mutated); this
    is the physical-truth correction (e.g. the start-form default was left
    unedited but the operator charged a different amount) — it drives
    ``weight_loss_percent`` in its place when present. The SPA must show
    BOTH the frozen and corrected values with which one is driving the
    percentage explicit, never a silent swap."""
    weight_loss_percent: float | None = None
    """Derived roast weight loss % — ``(charge - roasted) / charge * 100`` (#388),
    ``None`` until the roasted weight is entered. Predominantly moisture but also
    dry-matter loss (CO₂, volatiles, chaff), so NOT pure water loss. ``charge``
    is ``corrected_charge_grams`` when present (#520), else the frozen
    ``profile.bean_weight_grams``."""
    development_percent: float | None = None
    advisor_consults: int = 0
    """Total persisted advisor consults for this run (#184), aggregated
    server-side from ``advisor_decisions``. Mirrors the history advisor column's
    consult count, which until now the SPA derived per-row from
    ``GET /api/roasts/{id}/timeline`` (an N+1). ``0`` for a run that never
    consulted the advisor (back-compat: pre-advisor runs render "no advice")."""
    advisor_clamped: int = 0
    """Of this run's consults, how many produced a ``CLAMP`` safety verdict
    (#184) — counted per consult against the latest safety evaluation at the
    consult's tick, matching the SPA's prior client-side join. ``0`` when none."""
    advisor_rejected: int = 0
    """Of this run's consults, how many produced a ``REJECT`` safety verdict
    (#184), counted as in :attr:`advisor_clamped`. ``0`` when none."""
    advisor_failed: int = 0
    """Of this run's consults, how many did NOT return a usable decision (#184) —
    a ``timeout`` / ``malformed`` / ``provider_error`` status. ``0`` when none."""
    ambient_temp_c: float | None = None
    """Ambient temperature in Celsius, captured once at charge (#342, D85), or
    ``None`` when never captured. See :attr:`RoastDetail.ambient_temp_c`."""
    ambient_humidity_pct: float | None = None
    """Ambient relative humidity percentage at charge (#342, D85), or ``None``."""
    ambient_pressure_hpa: float | None = None
    """Ambient barometric pressure in hectopascals at charge (#342, D85), or
    ``None``."""
    excluded: bool = False
    """Reversible soft-exclude flag (#582), for filtering a bad-DATA roast out
    of history/stats/the learning corpus without deleting it. Always ``False``
    here: :meth:`~roastpilot_agent.store.RoastStore.list_runs` filters
    ``excluded=1`` rows out of this list entirely — a discarded run never
    appears in history. See :attr:`RoastDetail.excluded`, which a direct link
    to a discarded run still surfaces as ``True``."""


class RoastHistory(BaseModel):
    """``GET /api/roasts`` envelope."""

    runs: list[RoastSummary]


class OperatorAction(Enum):
    """The operator actions the API accepts (plan §6 enum).

    Plain ``Enum`` (D15): the SPA sends these wire forms, but a string
    comparison against a member in core logic is a pyright strict error.

    "Recovery-only" in plan §6 means *manual fallback*, not a single phase, and
    the two are not symmetric (see ``safety.COMMAND_PHASE_MATRIX``):
    ``mark_beans_added`` is the manual-T0 fallback accepted only in
    ``preheating`` (NOT in ``operator_recovery_required``), while
    ``start_cooling`` is accepted in ``cooling`` or ``operator_recovery_required``.
    ``pause_advisory`` / ``resume_advisory`` / ``acknowledge_recovery`` /
    ``acknowledge_fault`` are control actions with no direct MCP write.

    ``acknowledge_fault`` (#206) finalises an operable-faulted run: a fault no
    longer auto-finalises the run (so the operator can still engage/stop cooling
    on a physically-running machine), and acknowledging it is what stamps the
    ``faulted`` outcome and stops the loop. It is enabled iff the phase is
    ``faulted`` (mirror of ``acknowledge_recovery`` vs ``operator_recovery_required``).

    Declared here (before :class:`RoastDetail`) because that response model's
    ``enabled_actions`` field references it (E10 option (a))."""

    MARK_BEANS_ADDED = "mark_beans_added"
    MARK_FIRST_CRACK = "mark_first_crack"
    PAUSE_ADVISORY = "pause_advisory"
    RESUME_ADVISORY = "resume_advisory"
    DROP_BEANS = "drop_beans"
    START_COOLING = "start_cooling"
    STOP_COOLING = "stop_cooling"
    EMERGENCY_STOP = "emergency_stop"
    ACKNOWLEDGE_RECOVERY = "acknowledge_recovery"
    ACKNOWLEDGE_FAULT = "acknowledge_fault"


def _empty_actions() -> list[OperatorAction]:
    """Typed default factory for ``RoastDetail.enabled_actions`` (keeps pyright
    strict from inferring ``list[Unknown]`` off the bare ``list`` builtin)."""
    return []


class RoastDetail(BaseModel):
    """``GET /api/roasts/{id}``: profile, phase, outcome, export manifest.

    ``enabled_actions`` is the operator actions valid in the current phase,
    derived read-only from the safety command×phase matrix (E10 option (a)): the
    SPA's action bar mirrors this server-provided set rather than re-deriving a
    command×phase matrix client-side (the no-hardcoded-matrix invariant). It is a
    projection of phase, not persisted state; the live SSE ``phase_changed`` frame
    re-sends it so the bar updates on every transition.
    """

    id: str
    agent_phase: RoastPhase
    profile: RoastProfile
    outcome: Literal["completed", "aborted", "faulted"] | None = None
    started_at_utc: str
    completed_at_utc: str | None = None
    fault_reason: str | None = None
    rating: int | None = None
    notes: str | None = None
    roasted_weight_grams: float | None = None
    """Operator-entered roasted-out weight in grams (#388), or ``None`` when not
    yet weighed. The green/charge weight is ``profile.bean_weight_grams``."""
    corrected_charge_grams: float | None = None
    """Operator-entered CORRECTED charge/green weight in grams (#520), or
    ``None`` when never corrected. ``profile.bean_weight_grams`` stays what the
    controller/advisor actually ran with (never mutated); this is the
    physical-truth correction and drives ``weight_loss_percent`` in its place
    when present. The SPA must show BOTH values with which one is driving the
    percentage explicit, never a silent swap."""
    weight_loss_percent: float | None = None
    """Derived roast weight loss % — ``(charge - roasted) / charge * 100`` (#388),
    ``None`` until the roasted weight is entered. Predominantly moisture but also
    dry-matter loss (CO₂, volatiles, chaff), so NOT pure water loss. ``charge``
    is ``corrected_charge_grams`` when present (#520), else
    ``profile.bean_weight_grams``."""
    export_manifest: LogManifest | None = None
    enabled_actions: list[OperatorAction] = Field(default_factory=_empty_actions)
    mic_status: MicStatus | None = None
    """Capture-alive mic / first-crack health (#197), mirroring the
    ``enabled_actions`` server-derived precedent (D25): the SPA reads it
    read-only to render the mic icon. Populated only for the *active* run from
    the live MCP first-crack status; ``None`` for historical runs read from the
    store (the capture-alive status is transient, not persisted)."""
    ambient_temp_c: float | None = None
    """Ambient temperature in Celsius, captured once at charge (#342, D85), or
    ``None`` when never captured (an ambient-disabled/unavailable MCP config, a
    pre-charge run, or a pre-#342 row). Read-only corpus metadata: the MCP owns
    ambient, no safety gate or control path reads this field."""
    ambient_humidity_pct: float | None = None
    """Ambient relative humidity percentage at charge (#342, D85), or ``None``
    — same capture/back-compat rules as :attr:`ambient_temp_c`."""
    ambient_pressure_hpa: float | None = None
    """Ambient barometric pressure in hectopascals at charge (#342, D85), or
    ``None`` — same capture/back-compat rules as :attr:`ambient_temp_c`."""
    instance_id: str | None = None
    """The server process's ``instance_id`` (#516) at the moment this
    ``RoastDetail`` was served — mirrors ``HealthResponse.instance_id``, NOT
    persisted with the run (a live, process-scoped value, populated by
    :meth:`RoastService`'s response construction, not a store read). The
    start-roast confirm path captures this from the ``201`` response and
    compares it against a subsequent fresh ``/api/health`` read on arrival at
    ``/live`` — a mismatch means a DIFFERENT server process answered than the
    one that accepted the start, the #513 port-impostor signature. ``None``
    is a legitimate value (e.g. a pre-#516 fixture) and must never itself be
    treated as a mismatch."""
    excluded: bool = False
    """Reversible soft-exclude flag (#582) — ``True`` when the operator has
    discarded this roast as bad-data (beans fine, but e.g. a detector-missed
    first crack polluted the derived DTR). Unlike :attr:`RoastSummary.excluded`
    (always ``False`` — a discarded run never appears in the history list at
    all), this DOES surface ``True``:
    :meth:`~roastpilot_agent.store.RoastStore.read_run` still returns a
    discarded run so a direct link works, carrying the flag so the detail page
    can render a "Discarded" indicator + a restore action. The run's
    telemetry, events, safety/advisor/command trail, and any exported audio
    are all untouched — this is a soft flag, never a delete."""


class TelemetryPoint(BaseModel):
    """One persisted telemetry snapshot (plan §5 ``telemetry_snapshots``)."""

    tick: int
    elapsed_seconds: float | None = None
    charge_elapsed_seconds: float | None = None
    """Seconds since charge (T0) at this snapshot — the operator-facing roast
    clock (#308), persisted so the REST telemetry series re-origins the chart
    x-axis at charge (0:00) on a history/reload read, not only live over SSE.
    ``None`` before charge (and for pre-#308 rows). Distinct from
    ``elapsed_seconds`` (serve/run-referenced — the chart's raw x lead-in)."""
    agent_phase: RoastPhase
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None
    heat_level_percent: int | None = None
    fan_level_percent: int | None = None
    cooling_on: bool | None = None
    development_percent: float | None = None
    post_fc_recovery_enabled: bool | None = None
    """Resolved D96 flag for this persisted row; ``None`` for pre-v16 data."""
    post_fc_heat_authority_state: PostFcHeatAuthorityState | None = None
    post_fc_ror_setpoint_c_per_min: float | None = None
    post_fc_smoothed_ror_c_per_min: float | None = None
    post_fc_effective_heat_ceiling_percent: int | None = None


class TelemetrySeries(BaseModel):
    """``GET /api/roasts/{id}/telemetry``: a downsampled snapshot series.

    ``downsample`` is the sampling stride applied to the tick-ordered rows
    (``1`` returns every snapshot, ``5`` every fifth). The first snapshot is
    always retained so the series start is stable."""

    run_id: str
    downsample: int = Field(ge=1)
    point_count: int
    points: list[TelemetryPoint]


# Decision-trace wire forms — see the module note above on why these are
# literals, not imported enums.
TimelineVerdict = Literal["allow", "clamp", "reject", "recovery", "fault", "emergency_stop"]
AdvisorTraceStatus = Literal["ok", "timeout", "malformed", "provider_error"]
CommandTraceStatus = Literal["ok", "failed"]
CommandTraceSource = Literal["policy", "advisor", "operator", "safety", "recovery"]


class TimelineEvent(BaseModel):
    """One agent-level event in the decision trace (plan §5 ``roast_events``)."""

    kind: RoastEventKind
    source: RoastEventSource
    monotonic_seconds: float | None = None
    recorded_at_utc: str
    payload: dict[str, Any] | None = None


class TimelineSafetyEvaluation(BaseModel):
    """One safety verdict in the decision trace (plan §5 ``safety_evaluations``)."""

    tick: int
    rule: str
    verdict: TimelineVerdict
    input_heat: int | None = None
    input_fan: int | None = None
    adjusted_heat: int | None = None
    adjusted_fan: int | None = None
    reason: str
    recorded_at_utc: str


class TimelineAdvisorDecision(BaseModel):
    """One advisory outcome in the decision trace (plan §5 ``advisor_decisions``).

    ``safety_evaluation_id`` links to the :class:`TimelineSafetyEvaluation` the
    call produced (#167), so the FE can join an advisor decision to its verdict;
    ``None`` only for rows persisted before the FK was wired.
    """

    tick: int
    provider: str
    model: str
    prompt_version: str
    latency_ms: int | None = None
    status: AdvisorTraceStatus
    decision: dict[str, Any] | None = None
    safety_evaluation_id: int | None = None
    recorded_at_utc: str


class TimelineCommand(BaseModel):
    """One executed/failed MCP command in the decision trace (plan §5
    ``command_log``)."""

    tick: int
    tool: RoastCommand
    source: CommandTraceSource
    status: CommandTraceStatus
    args: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    recorded_at_utc: str


class RoastTimeline(BaseModel):
    """``GET /api/roasts/{id}/timeline``: the decision trace (plan §6) —
    roast events, safety verdicts, advisor decisions, and the command trail,
    each tick/insertion-ordered. Also the talk-demo data."""

    run_id: str
    events: list[TimelineEvent]
    safety_evaluations: list[TimelineSafetyEvaluation]
    advisor_decisions: list[TimelineAdvisorDecision]
    commands: list[TimelineCommand]


def weight_loss_percent(
    *, charge_weight_grams: float, roasted_weight_grams: float | None
) -> float | None:
    """Roast weight loss as a percentage of the green/charge weight (#388, D42).

    ``(charge - roasted) / charge * 100`` — the objective roast-degree/consistency
    signal that partners the subjective operator rating as a D42 corpus label.

    This is **weight loss %**, predominantly moisture but also dry-matter loss
    (CO₂, volatiles, chaff), so it is NOT pure "water loss" — true moisture loss
    would need green-vs-roasted moisture readings (out of scope). Shared by the
    store read path and the D44 fixture exporter so the corpus label and the UI
    agree on one derivation.

    Args:
        charge_weight_grams: The green/charge weight in (``RoastProfile.bean_weight_grams``).
        roasted_weight_grams: The operator-entered roasted-out weight, or ``None``.

    Returns:
        The weight-loss percentage rounded to two decimals, or ``None`` when the
        roasted weight is absent or either weight is non-positive (an unusable
        denominator / un-weighed roast yields no percentage).
    """
    if roasted_weight_grams is None or charge_weight_grams <= 0 or roasted_weight_grams <= 0:
        return None
    if roasted_weight_grams > charge_weight_grams:  # tare/scale error; physically impossible
        return None
    return round((charge_weight_grams - roasted_weight_grams) / charge_weight_grams * 100.0, 2)


class OperatorRatingRequest(BaseModel):
    """``POST /api/roasts/{id}/rating`` body (plan §6: ``{stars, notes}``)."""

    stars: Literal[1, 2, 3, 4, 5]
    notes: str | None = None


class RoastedWeightRequest(BaseModel):
    """``POST /api/roasts/{id}/roasted-weight`` body (#388).

    The operator-entered roasted-OUT weight in grams, captured post-roast after
    weighing — the same completion-only lifecycle as the rating. Must be > 0; the
    server derives weight-loss % from it against the frozen charge weight.
    """

    roasted_weight_grams: float = Field(gt=0)


class ChargeWeightRequest(BaseModel):
    """``POST /api/roasts/{id}/charge-weight`` body (#520).

    An operator correction to the CHARGE/green weight, for when the start-form
    default was left unedited but the operator actually charged a different
    amount (roast 13: charged 255 g, the form still had the 250 g seed
    default). Same completion-only lifecycle as the rating/roasted-weight.
    Must be > 0; the server also rejects a value below the roasted-out weight
    (physically impossible — the corrected charge cannot be less than what
    came out) as a 409, mirroring :class:`RoastedWeightRequest`'s own
    roasted-exceeds-charge check in the other direction.
    """

    corrected_charge_grams: float = Field(gt=0)


class ClearStaleSessionRequest(BaseModel):
    """``POST /api/roasts/{id}/clear-stale-session`` body (#525).

    Finalises a prior-session run row that is stranded open (``outcome IS
    NULL``) but is NOT this process's tracked active run and shows no recent
    telemetry — see :meth:`~roastpilot_agent.api.RoastService.clear_stale_session`
    for the full gate. A pure store write: it issues no MCP command and never
    touches heat, fan, or cooling. ``reason`` is required (no silent
    no-reason clears) and is recorded verbatim (post-strip) on the
    ``operator_actions`` audit row, whether the request is accepted or
    rejected.
    """

    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _strip_and_require_reason(cls, value: str) -> str:
        """Reject a whitespace-only reason server-side (PR #548 round-1 P3):
        ``min_length=1`` alone lets a direct API caller (bypassing the FE's
        own ``.trim()``) send ``"   "`` and pass. This is the audit CONTRACT,
        not just the UI's convenience gate — a caller that skips the browser
        entirely must face the same requirement. Stores the STRIPPED value
        (mirrors :class:`RoastProfile`'s ``_strip_and_require_content``) so a
        padded reason is never persisted padded."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class ClearStaleSessionResult(BaseModel):
    """The outcome of a successful :class:`ClearStaleSessionRequest` (#525).

    Always ``outcome="aborted"`` — this action only ever finalises a
    stranded row as abandoned; it never reclassifies what happened during
    the run (``agent_phase``/``fault_reason`` are left untouched)."""

    run_id: str
    outcome: Literal["aborted"]
    completed_at_utc: str


class HardwareClearAcknowledgementRequest(BaseModel):
    """Explicit operator confirmation after an unconfirmed MCP teardown (#668).

    ``hardware_clear`` must be the literal value ``true`` so a retry of another
    request can never be mistaken for this acknowledgement. ``reason`` is
    stripped and required for the process-global operator audit row.
    """

    hardware_clear: StrictBool
    teardown_incident_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("hardware_clear")
    @classmethod
    def _require_hardware_clear(cls, value: bool) -> bool:
        """Require the exact JSON boolean ``true``, without coercion."""
        if value is not True:
            raise ValueError("must be true")
        return value

    @field_validator("reason")
    @classmethod
    def _strip_and_require_reason(cls, value: str) -> str:
        """Reject and avoid persisting an empty or padded audit reason."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class HardwareClearAcknowledgementResult(BaseModel):
    """Successful process-global hardware-clear acknowledgement (#668)."""

    result: Literal["accepted"] = "accepted"
    hardware_clear: Literal[True] = True
    teardown_incident_id: str
    fresh_spawn_permitted: Literal[True] = True


# --- #522 (D91): structured tasting entries ---
#
# The E14 corpus starts now: every roast without a tasting is a lost label.
# Constrained ``Literal`` vocabularies (matching ``BeanSpecies`` /
# ``ProcessingMethod`` above), deliberately NOT ``models.py`` ``Enum``s — tasting
# attributes are not safety-bearing, and an enum here would trip the
# safety-reviewer escalation for no reason. ``"other"`` is the escape hatch on
# ``BrewMethod`` so the value stays a closed set.
BrewMethod = Literal[
    "espresso", "pour_over", "french_press", "aeropress", "moka_pot", "drip", "cupping", "other"
]

#: Positive attribute tags (sweetness/acidity/body — D91 §4). A tasting may tag
#: zero or more; free-text nuance still lives in ``notes``.
TastingAttribute = Literal["sweetness", "acidity", "body"]

#: Defect tags (D91 §4) — the roast-13 "flat → grassy" refinement is exactly the
#: signal this vocabulary makes computable for E14's synthesis.
TastingDefect = Literal["grassy", "baked", "bitter", "flat"]


def _empty_attributes() -> list[TastingAttribute]:
    """Typed default factory for the optional attribute-tag list fields below
    (mirrors ``_empty_actions`` — keeps pyright strict from inferring
    ``list[Unknown]`` off the bare ``list`` builtin)."""
    return []


def _empty_defects() -> list[TastingDefect]:
    """Typed default factory for the optional defect-tag list fields below."""
    return []


class TastingEntryRequest(BaseModel):
    """``POST /api/roasts/{id}/tastings`` body (#522, D91).

    A revisit tasting (e.g. the roast-13 same-evening-vs-hours-later
    refinement) is submitted as an ADDITIONAL entry, never an overwrite — the
    endpoint always inserts a new ``roast_tastings`` row. Every field beyond
    ``stars`` is optional so entry friction stays near zero (the operator rates
    from the phone post-tasting): stars + notes alone is still a valid tasting.
    """

    stars: Literal[1, 2, 3, 4, 5]
    notes: str | None = None
    tasted_at_utc: str | None = None
    """UTC ISO-8601 timestamp of this tasting, distinct from ``recorded_at_utc``
    (when the entry was saved) — the degassing offset (roast 13's same-evening
    "flat" vs. hours-later "grassy") is a real confound, so the roast-relative
    freshness must be computable from the tasting instant, not the save instant.
    ``None`` when the operator does not supply one; the server does NOT default
    it to "now" (see :meth:`RoastStore.add_tasting`) so an unset value stays
    honestly unknown rather than silently wrong. Validated and normalized to a
    UTC-offset ISO-8601 string (see :func:`_normalize_tasted_at`) — this field
    is the exact degassing signal #522 exists to capture, so an unparseable or
    ambiguous-offset value must fail loudly (422) rather than poison the corpus
    label silently."""
    brew_method: BrewMethod | None = None
    grind_note: str | None = None
    attributes: list[TastingAttribute] = Field(default_factory=_empty_attributes)
    """Positive attribute tags. Deduplicated on validation, preserving
    first-occurrence order (see :func:`_dedupe_tags`) — a repeated tag is
    friendlier normalized away than rejected, and the corpus never
    double-counts the same signal from one entry."""
    defects: list[TastingDefect] = Field(default_factory=_empty_defects)
    """Defect tags, deduplicated the same way as :attr:`attributes`."""

    @field_validator("tasted_at_utc")
    @classmethod
    def _validate_tasted_at(cls, value: str | None) -> str | None:
        return _normalize_tasted_at(value)

    @field_validator("attributes", "defects")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return _dedupe_tags(value)


#: Clock-skew tolerance for the future-timestamp guard on tasted_at_utc
#: (#522, Codex round 3): an honest operator client whose clock runs a little
#: ahead of the server's should not 422 for that alone. Small enough to still
#: catch a materially-wrong (e.g. wrong-year) future value.
_TASTED_AT_FUTURE_TOLERANCE = timedelta(minutes=5)


def _normalize_tasted_at(value: str | None) -> str | None:
    """Parse and UTC-normalize an operator-supplied tasting timestamp (#522).

    Accepts any ISO-8601 string ``datetime.fromisoformat`` can parse,
    including a naive (no offset) value or one with a non-UTC offset. A naive
    value is assumed to already be UTC (rather than silently guessing a local
    zone the server has no way to know); an offset value is converted to UTC.
    The result always round-trips through :meth:`datetime.isoformat`, matching
    the ``_utc_now()`` format every other persisted timestamp in this schema
    uses.

    Args:
        value: The raw operator-supplied string, or ``None``.

    Returns:
        The UTC-normalized ISO-8601 string, or ``None`` when ``value`` is
        ``None``.

    Raises:
        ValueError: ``value`` is not a parseable ISO-8601 datetime, is a bare
            date with no time component, or is materially in the future
            relative to when this request is being validated — Pydantic turns
            this into a 422 at the API boundary, so a malformed,
            under-specified, or impossible instant is rejected rather than
            persisted as-is and silently poisoning the degassing-offset
            corpus label this field exists to capture.
    """
    if value is None:
        return None
    # A bare date ("2026-07-13", no "T" separator) is technically a valid
    # ISO-8601 *date*, and datetime.fromisoformat happily parses it as
    # midnight — but a silently-invented midnight would shift the degassing
    # offset by up to 24 hours. Reject it explicitly rather than accept a
    # value the operator's client never intended as an exact instant; every
    # real instant carries a "T" time separator (the FE always sends one).
    if "T" not in value:
        raise ValueError(
            f"tasted_at_utc must include a time component (e.g. "
            f"'2026-07-13T18:00:00'), not a bare date: {value!r}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"tasted_at_utc is not a valid ISO-8601 datetime: {value!r}") from exc
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    # A tasting materially in the future is physically impossible (the
    # operator cannot taste beans before tasting them) — reject it, with a
    # small clock-skew tolerance so an honest client whose clock runs a
    # little ahead of the server's isn't 422'd for that alone. The lower
    # bound (vs. the run's own completed_at_utc) is enforced separately at
    # the API layer, where completed_at_utc is available.
    if parsed > datetime.now(UTC) + _TASTED_AT_FUTURE_TOLERANCE:
        raise ValueError(f"tasted_at_utc {value!r} is in the future")
    return parsed.isoformat()


def _dedupe_tags(values: list[str]) -> list[str]:
    """Deduplicate a tag list, preserving first-occurrence order (#522).

    A repeated tag (e.g. an accidental double-tap on a toggle button) is
    normalized away here rather than rejected — friendlier for the operator,
    and it keeps the corpus from double-counting the same signal within one
    tasting entry.

    Args:
        values: The raw tag list (``TastingAttribute`` or ``TastingDefect``
            values, typed as ``str`` here since the validator runs identically
            over either field).

    Returns:
        The same values with duplicates removed, in first-occurrence order.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


class RoastTasting(BaseModel):
    """One persisted tasting entry (#522, D91 — ``roast_tastings`` row)."""

    id: int
    tasted_at_utc: str | None = None
    recorded_at_utc: str
    stars: int
    notes: str | None = None
    brew_method: BrewMethod | None = None
    grind_note: str | None = None
    attributes: list[TastingAttribute] = Field(default_factory=_empty_attributes)
    defects: list[TastingDefect] = Field(default_factory=_empty_defects)


class TastingList(BaseModel):
    """``GET /api/roasts/{id}/tastings`` envelope (#522) — every tasting entry
    for the run, oldest first (the natural revisit order: first taste, then any
    later refinement)."""

    run_id: str
    tastings: list[RoastTasting]


# --- E7-S2: operator action queue (component plan §6) ---


class OperatorActionRequest(BaseModel):
    """``POST /api/roasts/{id}/operator-actions`` body (plan §6:
    ``{action, payload?}``)."""

    action: OperatorAction
    payload: dict[str, Any] | None = None


class OperatorActionResult(BaseModel):
    """The outcome of submitting an operator action (plan §6).

    ``result`` mirrors the persisted ``operator_actions.result`` vocabulary.
    In E7 the queue resolves ``accepted`` (phase-valid, queued for the
    controller) or ``rejected`` (declined by safety policy, with the reason);
    ``failed`` is reserved for an execution failure once the controller drains
    the queue and writes MCP (E9 vertical slice). ``queued`` is true only when
    the action was placed on the controller queue."""

    action: OperatorAction
    result: Literal["accepted", "rejected", "failed"]
    reason: str
    queued: bool


# --- E7-S3: SSE event stream (component plan §6) ---
#
# The typed SSE event vocabulary is E7's most important output: the E9 vertical
# slice and the E10 SPA both render from it, so the event-type set and the
# envelope are the stable contract. Every RoastEventKind the controller emits
# flows to the stream, plus the two transport-only events the API itself
# originates — per-tick ``telemetry`` and the ``heartbeat`` keepalive.


class SseEventType(Enum):
    """The ``event:`` field of every SSE frame (plan §6).

    The superset of :class:`RoastEventKind` (so every controller event reaches
    the SPA, including ``recovery_acknowledged``) plus the two transport-only
    events the API originates: ``telemetry`` (every tick) and ``heartbeat``
    (15 s keepalive). Values match ``RoastEventKind`` byte-for-byte, so
    ``SseEventType(kind.value)`` maps a controller event to its frame type. A
    test pins this superset relationship so the two never drift."""

    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    CHARGE_GUIDANCE = "charge_guidance"
    T0_DETECTED = "t0_detected"
    TURNING_POINT = "turning_point"
    DRYING_END = "drying_end"
    FIRST_CRACK = "first_crack"
    ADVISORY = "advisory"
    COMMAND_EXECUTED = "command_executed"
    COMMAND_FAILED = "command_failed"
    SAFETY_ALERT = "safety_alert"
    FAULT = "fault"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_ACKNOWLEDGED = "recovery_acknowledged"
    LOGS_EXPORTED = "logs_exported"
    RUN_COMPLETED = "run_completed"
    TELEMETRY = "telemetry"
    HEARTBEAT = "heartbeat"


class TelemetryEventData(BaseModel):
    """Payload of the per-tick ``telemetry`` SSE event (plan §6).

    The live reading the SPA renders each tick: the agent phase plus the
    current telemetry and applied heat/fan. The controller constructs it from
    the tick's ``RoastTelemetry`` + phase + commanded levels and publishes it
    through the broadcaster (E9); the SPA never infers phase locally."""

    agent_phase: RoastPhase
    bean_temp_c: float
    env_temp_c: float
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None
    heat_percent: int | None = None
    fan_percent: int | None = None
    cooling_on: bool = False
    elapsed_seconds: float | None = None
    charge_elapsed_seconds: float | None = None
    """Seconds since charge (T0) — the operator-facing roast clock (#308). ``None``
    before charge (the SPA shows '—' / no roast time during preheat), and frozen
    at the drop value in cooling. Server-authoritative (the controller's
    ``_charge_elapsed_seconds``, the same charge/T0 instant the advisor's DTR uses).
    The SPA renders ROAST TIME from this with 0:00 = charge and re-origins the
    chart x-axis to charge. **Distinct from** ``elapsed_seconds``
    (serve/run-referenced — the chart's raw x lead-in, kept so the SPA can still
    draw the pre-charge preheat curve)."""
    development_elapsed_seconds: float | None = None
    """Seconds since first crack — the live development clock (#220). ``None``
    before first crack. Server-authoritative (the controller's
    ``_development_elapsed_seconds``): the SPA renders this directly rather than
    deriving it from the FC event vs ``elapsed_seconds`` (the #112 gap)."""
    development_percent: float | None = None
    """DTR (development time ratio) as a *percentage* of the WHOLE roast (#220):
    ``development_elapsed / charge_elapsed * 100``. Charge-referenced
    (consistent with the advisor's DTR, #219) — NOT the run/serve clock.
    ``None`` before first crack (or before charge). A live readout DISTINCT from
    ``development_elapsed_seconds``: one is a duration, the other a ratio."""
    post_fc_recovery_enabled: bool = False
    """Resolved D96 flag for this run, separate from the authority state so an
    operator can distinguish feature OFF from ARMED-but-HOLDING."""
    post_fc_heat_authority_state: PostFcHeatAuthorityState | None = None
    """Controller-owned D96 authority state for the current DEVELOPMENT dwell."""
    post_fc_ror_setpoint_c_per_min: float | None = None
    post_fc_smoothed_ror_c_per_min: float | None = None
    post_fc_effective_heat_ceiling_percent: int | None = None
    t0_detected: bool = False
    first_crack_detected: bool = False
    mic_status: MicStatus | None = None
    """Capture-alive mic / first-crack health (#197), server-derived and
    read-only on the SPA — mirrors the ``enabled_actions`` precedent (D25).
    ``None`` when no first-crack status is available this tick."""
    ambient_temp_c: float | None = None
    """Live/latest ambient temperature in Celsius (#464, D86), mirrored each
    tick from the MCP's ~30 s-cached ``ambient_status`` — DISTINCT from the
    charge-time ``RoastDetail.ambient_temp_c`` (#342, D85), which is a one-time
    capture persisted at charge. ``None`` when ambient is uncaptured, disabled,
    or unavailable this tick. Observability-only: no safety gate or control
    path reads this field."""
    ambient_humidity_pct: float | None = None
    """Live/latest ambient relative humidity percentage (#464, D86) — same
    mirrored-each-tick / observability-only rules as :attr:`ambient_temp_c`."""
    ambient_pressure_hpa: float | None = None
    """Live/latest ambient barometric pressure in hectopascals (#464, D86) —
    same mirrored-each-tick / observability-only rules as
    :attr:`ambient_temp_c`."""


class SseEvent(BaseModel):
    """One typed Server-Sent Event frame (plan §6).

    The stable envelope the SPA parses: a typed ``event`` discriminator and a
    JSON ``data`` payload. ``data`` carries the API-owned payloads
    (``TelemetryEventData`` dumped to a dict) and the controller event payloads
    verbatim (already JSON-safe dicts at their emit sites). ``id`` is an
    optional monotonic sequence the broadcaster stamps for ordering/dedup."""

    event: SseEventType
    data: dict[str, Any] = Field(default_factory=dict)
    id: int | None = None

    def render(self) -> str:
        """Serialize to the SSE wire format (``id:``/``event:``/``data:`` +
        blank-line terminator)."""
        lines: list[str] = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event.value}")
        lines.append(
            f"data: {json.dumps(sanitize_non_finite(self.data), sort_keys=True, allow_nan=False)}"
        )
        return "\n".join(lines) + "\n\n"


def sanitize_non_finite(value: object) -> object:
    """Return a JSON-ready copy with every non-finite float replaced by ``None``.

    Dictionary keys are copied but not sanitised, while dictionaries, lists, and
    tuples are rebuilt so sanitising a shared event or response cannot mutate its
    source. The wire contract requires string keys; an out-of-contract non-finite
    float key therefore fails closed at the strict JSON backstop.

    Args:
        value: Arbitrarily nested value headed for a JSON wire boundary.

    Returns:
        The original scalar or a recursively sanitised container copy.
    """
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {key: sanitize_non_finite(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return [sanitize_non_finite(item) for item in items]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
