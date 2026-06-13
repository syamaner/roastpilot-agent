"""Shared Pydantic models and enums (component plan §4).

Scaffold stubs only — the full model set (including MCP state mirrors and
SSE event payloads) lands in E2. All temperatures are Celsius everywhere.

The shared enums here are plain ``Enum``, deliberately not ``StrEnum``:
comparing a member against a raw string must be a pyright strict error
(``reportUnnecessaryComparison``), per the AGENTS.md typed-vocabulary
invariant. Use ``.value`` at serialization boundaries.
"""

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class RoastTelemetry(BaseModel):
    """Minimal controller-facing telemetry reading (E4).

    E5's typed MCP mirrors construct this from ``RoastSessionState``; the
    controller's tick pipeline consumes it. Derived metrics (RoR) are
    passed through from MCP, never recomputed (plan §2).
    """

    bean_temp_c: float
    env_temp_c: float
    age_seconds: float = 0.0
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None
    t0_detected: bool = False
    first_crack_detected: bool = False
    cooling_on: bool = False


class RoastProfile(BaseModel):
    """Minimal static roast profile (decision D7).

    No curve targets in M1: name, bean details, charge guidance range,
    initial heat/fan, target drop temperature, target development percent.
    The profile is frozen into ``roast_runs.profile_json`` at run start
    (plan §5); hardware safety limits live in config, not here.
    """

    name: str = Field(min_length=1)
    bean_origin: str = Field(min_length=1)
    bean_varietal: str | None = None
    bean_weight_grams: float = Field(gt=0)
    charge_guidance_min_c: float = 170.0
    # The guidance ceiling deliberately equals the pre-T0 safety bound
    # (config.SafetyLimits.pre_t0_max_bean_temp_c, default 200.0): operators
    # are guided to charge before the hard bound trips. A scaffold test pins
    # charge_guidance_max_c <= pre_t0_max_bean_temp_c; keep them in sync.
    charge_guidance_max_c: float = 200.0
    initial_heat_percent: int = Field(ge=0, le=100)
    initial_fan_percent: int = Field(ge=0, le=100)
    target_drop_temp_c: float = Field(gt=0)
    target_development_percent: float = Field(gt=0, lt=100)

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

    @model_validator(mode="after")
    def _check_guidance_range(self) -> "RoastProfile":
        """The charge guidance band must be a non-empty range."""
        if self.charge_guidance_min_c >= self.charge_guidance_max_c:
            raise ValueError(
                "charge_guidance_min_c must be below charge_guidance_max_c "
                f"({self.charge_guidance_min_c} >= {self.charge_guidance_max_c})"
            )
        return self


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
    """

    status: Literal["ok"] = "ok"
    version: str
    mcp_child: MCPChildStatus
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
    """History list item (plan §6: id, started, outcome, bean, rating, dev %)."""

    id: str
    started_at_utc: str
    completed_at_utc: str | None = None
    agent_phase: RoastPhase
    outcome: Literal["completed", "aborted", "faulted"] | None = None
    bean_origin: str
    bean_varietal: str | None = None
    rating: int | None = None
    development_percent: float | None = None


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
    ``pause_advisory`` / ``resume_advisory`` / ``acknowledge_recovery`` are
    control actions with no direct MCP write.

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
    export_manifest: LogManifest | None = None
    enabled_actions: list[OperatorAction] = Field(default_factory=_empty_actions)


class TelemetryPoint(BaseModel):
    """One persisted telemetry snapshot (plan §5 ``telemetry_snapshots``)."""

    tick: int
    elapsed_seconds: float | None = None
    agent_phase: RoastPhase
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None
    heat_level_percent: int | None = None
    fan_level_percent: int | None = None
    cooling_on: bool | None = None
    development_percent: float | None = None


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
    """One advisory outcome in the decision trace (plan §5 ``advisor_decisions``)."""

    tick: int
    provider: str
    model: str
    prompt_version: str
    latency_ms: int | None = None
    status: AdvisorTraceStatus
    decision: dict[str, Any] | None = None
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


class OperatorRatingRequest(BaseModel):
    """``POST /api/roasts/{id}/rating`` body (plan §6: ``{stars, notes}``)."""

    stars: Literal[1, 2, 3, 4, 5]
    notes: str | None = None


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
    t0_detected: bool = False
    first_crack_detected: bool = False


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
        lines.append(f"data: {json.dumps(self.data, sort_keys=True)}")
        return "\n".join(lines) + "\n\n"
