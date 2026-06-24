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
from urllib.parse import urlsplit

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
    export_manifest: LogManifest | None = None
    enabled_actions: list[OperatorAction] = Field(default_factory=_empty_actions)
    mic_status: MicStatus | None = None
    """Capture-alive mic / first-crack health (#197), mirroring the
    ``enabled_actions`` server-derived precedent (D25): the SPA reads it
    read-only to render the mic icon. Populated only for the *active* run from
    the live MCP first-crack status; ``None`` for historical runs read from the
    store (the capture-alive status is transient, not persisted)."""


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
    t0_detected: bool = False
    first_crack_detected: bool = False
    mic_status: MicStatus | None = None
    """Capture-alive mic / first-crack health (#197), server-derived and
    read-only on the SPA — mirrors the ``enabled_actions`` precedent (D25).
    ``None`` when no first-crack status is available this tick."""


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
