"""Deterministic controller (component plan §3–§4; orchestration plan
§ State Machine, § Controller Loop).

Owns the transition table (E4-S1), the monotonic fixed-rate tick scheduler
and tick pipeline (E4-S2), the T0 debounce (E4-S3), and restart recovery
(E4-S4). The RoastPhase vocabulary lives in models.py (D15) and is
re-exported here for plan §4 compatibility.

The advisor cannot trigger state transitions — structurally: no transition
API accepts advisor output, and T0/FC sources are additionally validated by
safety.evaluate_event_source (E3-S5). Restart never auto-resumes heat or
fan (``operator_recovery_required``).
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal, Protocol

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorMalformedOutputError,
    AdvisorUnsafeOutputError,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.coherence import (
    CoherenceDecision,
    LeverDirection,
    evaluate_lever_coherence,
)
from roastpilot_agent.config import ControllerConfig, LateMaillardTrim
from roastpilot_agent.control_policy import (
    PhaseControlLimits,
    PostFcFanSignal,
    RoastControlPolicy,
    TrimSignal,
)
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    AppliedRoasterState,
    DropReason,
    PostFcHeatAuthorityState,
    ReferenceRoast,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
    recording_origin_slug,
)
from roastpilot_agent.post_fc_control import (
    PostFcControllerState,
    PostFcControlOutput,
    PostFcRorController,
)
from roastpilot_agent.roast_history import (
    DecisionTraceEntry,
    RoastCurveSample,
    RoastHistory,
    RoastMilestone,
    RoastMilestoneKind,
    estimate_first_crack_eta_seconds,
)
from roastpilot_agent.safety import (
    COMMAND_PHASE_MATRIX,
    SafetyEvaluation,
    SafetyPolicy,
    SafetyVerdict,
)

__all__ = [
    "AUTO_ADVICE_PHASES",
    "TRANSITION_TABLE",
    "UNIVERSAL_TARGETS",
    "AdvisoryCallPolicy",
    "AdvisoryTrigger",
    "CommandExecutor",
    "ControllerSnapshot",
    "EventEmitter",
    "InvalidTransitionError",
    "RoastController",
    "RoastPhase",
    "SnapshotSink",
    "StateReader",
    "TickScheduler",
    "recording_origin_slug",
]

Clock = Callable[[], float]
AsyncSleep = Callable[[float], Awaitable[None]]


#: The controller is event-driven rather than log-driven. Logs here are reserved
#: for low-volume control diagnostics that must reach an operator but do not
#: warrant a new server event kind (which would expand the FE event contract).
_log = logging.getLogger(__name__)


# recording_origin_slug now lives in models.py (with RoastProfile) so the
# store's per-origin recording-count query (#385) can derive the same slug from a
# completed run's frozen profile without importing the controller. Re-exported
# here for the existing import path.


class InvalidTransitionError(Exception):
    """Raised when a phase transition is not in the transition table."""

    def __init__(self, current: RoastPhase, target: RoastPhase) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid transition: {current.value} -> {target.value}")


# Normal-path and operator-driven edges (plan §3 / orchestration plan
# § State Machine "Recommended transition ownership"; E4-S1 refinements
# recorded under plan §3). The universal edges `* -> faulted` and
# `* -> operator_recovery_required` are handled in
# RoastController.can_transition, not listed per row.
TRANSITION_TABLE: dict[RoastPhase, frozenset[RoastPhase]] = {
    # Operator starts a roast.
    RoastPhase.IDLE: frozenset({RoastPhase.STARTING}),
    # MCP session started successfully.
    RoastPhase.STARTING: frozenset({RoastPhase.PREHEATING}),
    # MCP reports confirmed T0 (debounced, E4-S3).
    RoastPhase.PREHEATING: frozenset({RoastPhase.ROASTING_PRE_FIRST_CRACK}),
    # First crack detected or operator override — or an operator
    # early-abort drop straight to cooling (the DROP_BEANS matrix row
    # allows the drop here; the table must support the resulting state,
    # safety review E4-S4).
    RoastPhase.ROASTING_PRE_FIRST_CRACK: frozenset({RoastPhase.DEVELOPMENT, RoastPhase.COOLING}),
    # Validated drop decision or operator drop.
    RoastPhase.DEVELOPMENT: frozenset({RoastPhase.COOLING}),
    # Cooling stopped and logs exported.
    RoastPhase.COOLING: frozenset({RoastPhase.COMPLETE}),
    # Run finalized: the controller returns to idle for the next run
    # (E4-S1 refinement, plan §3 note).
    RoastPhase.COMPLETE: frozenset({RoastPhase.IDLE}),
    # Operator acknowledgement ends a faulted run.
    RoastPhase.FAULTED: frozenset({RoastPhase.IDLE}),
    # Explicit operator action only (orchestration plan § Persistence:
    # resume, drop, cool, or end the run — never automatic). `starting`
    # is never a recovery target.
    RoastPhase.OPERATOR_RECOVERY_REQUIRED: frozenset(
        {
            RoastPhase.PREHEATING,
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            RoastPhase.DEVELOPMENT,
            RoastPhase.COOLING,
            RoastPhase.COMPLETE,
            RoastPhase.IDLE,
        }
    ),
}
"""Explicit transition table: maps each phase to its legal targets
(excluding the universal faulted/recovery edges). Every phase has a row;
a test pins completeness."""

#: Phases any state may fall into (plan §3: `* -> faulted`,
#: `* -> operator_recovery_required`). Self-transitions are not transitions.
UNIVERSAL_TARGETS: frozenset[RoastPhase] = frozenset(
    {RoastPhase.FAULTED, RoastPhase.OPERATOR_RECOVERY_REQUIRED}
)

#: Terminal HOLD phases the controller latches into. Once the tick loop has
#: failed closed into one of these (fail-safe already applied, fault/recovery
#: event already emitted on entry), every later tick is a no-op: it does NOT
#: re-read the (possibly dead) MCP, re-evaluate safety, or re-emit the terminal
#: event. Leaving these phases is an EXPLICIT operator action only
#: (``operator_acknowledge_fault`` → idle, ``operator_resume`` out of recovery,
#: ``operator_start_cooling`` from faulted/recovery, ``operator_emergency_stop``),
#: never a tick transition — so the latch never strands an operable-faulted run
#: (#206): emergency-stop, cooling, and acknowledge stay available throughout.
#: This kills the post-#206 "infinite error loop" where a sustained dead-MCP read
#: re-emitted the identical FAULT event every tick (roast 2, attempt 2).
#:
#: Bound to :data:`UNIVERSAL_TARGETS` by IDENTITY, not duplicated (PR review): the
#: phases the tick latches in ARE exactly the universal fall-into-from-anywhere
#: terminal phases, so a single source of truth keeps them from drifting apart if
#: one is ever extended. A test pins the equality so the coupling is intentional.
TERMINAL_LATCH_PHASES: frozenset[RoastPhase] = UNIVERSAL_TARGETS

#: Severity ranking of the terminal-stage verdicts, for the upward-only
#: escalation a latched controller still performs (safety-reviewer carry-forward
#: on #206). A latched controller re-reads + re-evaluates each tick and acts ONLY
#: when the new verdict is STRICTLY MORE SEVERE than the one it latched on — so a
#: ``faulted`` run whose (live) MCP then reports a hard-ceiling breach still
#: auto-escalates to the hardware emergency stop, while a sustained same-or-lesser
#: verdict (or a dead-MCP read) produces no re-emit and no re-fire. Only these
#: three verdicts ever latch a terminal phase; ALLOW/CLAMP/REJECT never do.
_TERMINAL_VERDICT_SEVERITY: dict[SafetyVerdict, int] = {
    SafetyVerdict.RECOVERY: 1,
    SafetyVerdict.FAULT: 2,
    SafetyVerdict.EMERGENCY_STOP: 3,
}


class StateReader(Protocol):
    """Reads the current roast telemetry (E5 wraps get_roast_state)."""

    async def read_telemetry(self) -> RoastTelemetry | None:
        """Return the latest telemetry, or None when no session exists."""
        ...


class CommandExecutor(Protocol):
    """Executes safety-approved roaster writes (E5 wraps the MCP tools)."""

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        """Start a new MCP roast session.

        Args:
            recording_origin: Optional origin slug for the export filename
                (v0.1.9 ``set_recording_metadata``, #176); ``None`` lets the MCP
                fall back to its default naming.
            recording_roast_num: Optional per-origin roast counter; ``None``
                falls back with ``recording_origin``.
        """
        ...

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        """Apply validated heat/fan targets."""
        ...

    async def mark_beans_added(self) -> None:
        """Record the manual beans-added (manual-T0 fallback) with MCP."""
        ...

    async def mark_first_crack(self) -> None:
        """Record the operator first-crack override with MCP."""
        ...

    async def drop_beans(self) -> AppliedRoasterState | None:
        """Drop the beans (normal drop/cooling transition).

        Returns the driver's applied post-drop heat/fan/cooling state (#507)
        — a drop changes those as a hardware side effect of the command
        itself, so the caller adopts this return value into its own
        commanded-value mirrors rather than assuming a constant. ``None``
        means the hardware command succeeded but its result payload could not
        be parsed (a malformed/out-of-contract MCP) — the caller proceeds
        exactly as on a normal success (the drop already happened), simply
        without adopting a value into the mirrors this tick.
        """
        ...

    async def start_cooling(self) -> None:
        """Start the cooling cycle (recovery action / post-drop fallback)."""
        ...

    async def stop_cooling(self) -> None:
        """Stop the cooling cycle."""
        ...

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
        """Fire the MCP emergency_stop command.

        Returns the driver's applied post-stop heat/fan/cooling state (#507),
        mirroring :meth:`drop_beans` — including the ``None``-on-malformed-
        payload contract."""
        ...


class SnapshotSink(Protocol):
    """Persists tick data (E6 implements over SQLite)."""

    async def persist_snapshot(self, telemetry: RoastTelemetry | None) -> None:
        """Persist the raw telemetry snapshot for this tick."""
        ...

    async def persist_evaluation(self, evaluation: SafetyEvaluation) -> int | None:
        """Persist a safety evaluation.

        Returns the persisted row id so an advisor decision can link to the
        verdict it produced (#167). Sinks that do not persist (tests, no-op
        sinks) may return ``None``.
        """
        ...

    async def persist_advisor_decision(
        self,
        *,
        descriptor: AdvisorDescriptor,
        context: AdvisorContext,
        latency_ms: int | None,
        decision: RoastDecision | None,
        status: AdvisorTraceStatus,
        safety_evaluation_id: int | None,
    ) -> None:
        """Persist one advisory outcome — the advisor decision trace (#167).

        Records the provider/model/prompt-version (from ``descriptor``), the
        call status, latency, the ``RoastDecision`` (``None`` on failure), and
        the id of the safety evaluation the call produced, so the trace joins
        each advisor decision to its verdict.
        """
        ...


class EventEmitter(Protocol):
    """Emits UI events (E7 implements over SSE)."""

    def emit(self, kind: RoastEventKind, payload: object) -> None:
        """Emit one event."""
        ...


class TickScheduler:
    """Monotonic fixed-rate scheduler (orchestration plan § Controller Loop).

    Target times advance by exactly the interval — work duration never
    accumulates drift. A slow tick produces positive jitter (recorded) and
    a zero-length sleep; subsequent ticks fall back onto the original
    schedule. Clock and sleep are injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        tick: Callable[[], Awaitable[None]],
        clock: Clock = time.monotonic,
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        self._interval = interval_seconds
        self._tick = tick
        self._clock = clock
        self._sleep = sleep
        self._running = False
        self.tick_count = 0
        self.last_jitter_seconds = 0.0
        self.max_jitter_seconds = 0.0

    @property
    def running(self) -> bool:
        """Whether the run loop is active."""
        return self._running

    def stop(self) -> None:
        """Stop after the current tick completes."""
        self._running = False

    async def run(self) -> None:
        """Run ticks until :meth:`stop` is called."""
        self._running = True
        scheduled = self._clock()
        while self._running:
            jitter = self._clock() - scheduled
            self.last_jitter_seconds = jitter
            self.max_jitter_seconds = max(self.max_jitter_seconds, jitter)
            await self._tick()
            self.tick_count += 1
            scheduled += self._interval
            await self._sleep(max(0.0, scheduled - self._clock()))


class AdvisoryTrigger(Enum):
    """Why the advisor was consulted on a given tick (D15: plain ``Enum``).

    Recorded on the ADVISORY event so the decision trace shows *why* advice
    was requested — talk material per the E9/E12 demo-asset plan, and the
    discriminator the call-frequency tests assert against.
    """

    MANUAL = "manual"
    PHASE_CHANGE = "phase_change"
    BEAN_TEMP_DELTA = "bean_temp_delta"
    ROR_DELTA = "ror_delta"
    MIN_INTERVAL = "min_interval"
    # NOTE: the ``near_fc`` trigger (D32/#191) is retired under D35 (#222) — it
    # only boosted *pre-FC* advisory cadence, and the advisor is no longer
    # consulted pre-FC. Kept out of the enum so a stale value cannot be emitted.


# The pre-first-crack phases the deterministic lever policy owns (D35 §3, #222):
# preheat and charge→FC. Before FC the controller drives heat/fan from the
# policy every tick and the free-form advisor is NOT consulted at all — neither
# automatically nor on a manual request (the LLM thrashed and baked the roast
# here, #218; there is no craft to add — max heat, low fan to FC). The advisor's
# first consult is at/after first crack (the post-FC loop is #223). Gating the
# advisor out of these phases makes #209's post-charge settle window a no-op
# pre-FC, as D35 requires (the window only ever suppressed *automatic* pre-FC
# advice; with no pre-FC advice at all it is inert).
_DETERMINISTIC_PRE_FC_PHASES: frozenset[RoastPhase] = frozenset(
    {RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK}
)
# Advice is only worth requesting in phases where its output (a heat/fan
# target) could legally execute — the SET_HEAT row of the command×phase
# matrix is the single source of truth, so this never drifts from safety.
_ADVICE_PHASES: frozenset[RoastPhase] = COMMAND_PHASE_MATRIX[RoastCommand.SET_HEAT]
# Phases the advisor is consulted in AUTOMATICALLY (D32 / #191 + D35 / #222): the
# deterministic pre-FC phases (preheat AND charge→FC) are excluded — pre-FC is
# deterministic and the advisor is not consulted there. With only DEVELOPMENT
# left, post-FC is where the LLM advises (#223).
# PUBLIC (#746): the roast-live launcher's banner reads this to report the model
# the advisor will ACTUALLY be asked for, so a per-phase model in a phase that
# never consults cannot be announced as the arm being run. Read-only — the
# gating itself stays owned by ``_maybe_run_advisory`` below.
AUTO_ADVICE_PHASES: frozenset[RoastPhase] = _ADVICE_PHASES - _DETERMINISTIC_PRE_FC_PHASES


class AdvisoryCallPolicy:
    """Decides when the advisor is consulted (orchestration plan § Advisory
    Call Frequency).

    Under D35 (#222) the advisor is consulted ONLY post-first-crack — the
    deterministic controller owns the levers before FC and the free-form advisor
    is gated out of every pre-FC phase. So this policy now governs the post-FC
    cadence only:

    - **preheat / charge→FC**: not automatic-advice phases (the deterministic
      pre-FC policy owns them; the advisor is not consulted there at all). The
      former post-charge settle window (#209) and near-FC boost (D32/#191) are
      retired here — they only ever shaped *pre-FC* advisory cadence, which no
      longer exists; #209 is now a no-op as D35 §2 mandates.
    - **development (post-FC)**: unthrottled (floor 0) — an automatic call fires
      on a meaningful change since the last call (a phase transition, a bean-temp
      move of ``advisory_min_temp_delta_c``, a RoR move of
      ``advisory_min_ror_delta_c_per_min``) or, failing those, the phase's
      consult floor.

    A manual operator request bypasses every gate, including phase scoping —
    however the controller does not route a manual pre-FC request here either
    (it is short-circuited before this policy is consulted, #222), so a manual
    request only reaches this policy post-FC.

    Pure and deterministic: :meth:`evaluate` only reads state, and the
    controller calls :meth:`note_call` after an actual consult to advance
    the baselines. Unit-testable in isolation by feeding scripted
    ``(phase, telemetry, now)`` sequences.
    """

    def __init__(self, config: ControllerConfig) -> None:
        self._config = config
        self._last_call_monotonic: float | None = None
        self._last_bean_temp_c: float | None = None
        self._last_bean_ror_c_per_min: float | None = None
        self._last_phase: RoastPhase | None = None

    def evaluate(
        self,
        *,
        phase: RoastPhase,
        telemetry: RoastTelemetry | None,
        now: float,
        manual_request: bool,
    ) -> AdvisoryTrigger | None:
        """Return the trigger to consult the advisor this tick, or ``None``.

        Manual wins unconditionally. Otherwise automatic triggers apply only
        in advice-applicable phases (post-FC only under D35, #222), evaluated
        most-meaningful first: a phase change (including the first consult in an
        advice phase), then the bean-temp and RoR deltas, then the
        minimum-interval heartbeat.
        """
        if manual_request:
            return AdvisoryTrigger.MANUAL
        if phase not in AUTO_ADVICE_PHASES:
            return None
        # First consult in an advice phase, or any phase transition since the
        # last call: ``_last_phase`` starts None, so the first eligible tick
        # establishes the baseline.
        if phase is not self._last_phase:
            return AdvisoryTrigger.PHASE_CHANGE
        # Logically redundant — note_call sets ``_last_phase`` and
        # ``_last_call_monotonic`` together, so a matched phase implies a
        # recorded call — but it narrows the Optional for the interval check
        # below and documents the invariant.
        if self._last_call_monotonic is None:
            return AdvisoryTrigger.PHASE_CHANGE
        # D35 §4-A / D40.5 (#276): a deliberate post-FC consult cadence. The
        # change-based triggers (temp/RoR delta) below would otherwise fire every
        # eligible tick on a fast-moving development curve, re-creating the
        # change-based-every-tick cadence (D32) that drove the #218 twiddling. A
        # minimum post-FC dwell suppresses BOTH the delta triggers and the
        # heartbeat until it elapses, so development consults run at the ~5 s
        # cadence the deadband judges the model's trajectory across. The first
        # consult in the phase (the PHASE_CHANGE above) is unaffected; a manual
        # request bypassed this whole method. Pre-FC phases never reach here.
        min_dwell = self._phase_min_consult_interval(phase)
        if min_dwell is not None and now - self._last_call_monotonic < min_dwell:
            return None
        if (
            telemetry is not None
            and self._last_bean_temp_c is not None
            and abs(telemetry.bean_temp_c - self._last_bean_temp_c)
            >= self._config.advisory_min_temp_delta_c
        ):
            return AdvisoryTrigger.BEAN_TEMP_DELTA
        if (
            telemetry is not None
            and telemetry.bean_ror_c_per_min is not None
            and self._last_bean_ror_c_per_min is not None
            and abs(telemetry.bean_ror_c_per_min - self._last_bean_ror_c_per_min)
            >= self._config.advisory_min_ror_delta_c_per_min
        ):
            return AdvisoryTrigger.ROR_DELTA
        # Phase-keyed consult floor (D32 / #171), resolved from the *current*
        # phase so it follows the roast forward: development 0 = unthrottled (a
        # 0 floor fires every eligible tick once the prior serial call returns,
        # so development consults run back-to-back at advisor latency). Only
        # post-FC phases reach here under D35 (#222). The change-based triggers
        # above still short-circuit sooner.
        floor = self._config.advisory_interval_for(phase)
        if floor is not None and now - self._last_call_monotonic >= floor:
            return AdvisoryTrigger.MIN_INTERVAL
        return None

    def _phase_min_consult_interval(self, phase: RoastPhase) -> float | None:
        """The minimum-dwell floor that gates EVERY automatic trigger in ``phase``.

        Distinct from :meth:`ControllerConfig.advisory_interval_for` (the
        MIN_INTERVAL heartbeat floor): this floor additionally suppresses the
        change-based triggers, so the post-FC cadence is a deliberate dwell rather
        than every-tick on a fast curve (D35 §4-A / D40.5, #276). Only DEVELOPMENT
        (the single post-FC advice phase under D35) carries a dwell; every other
        phase returns ``None`` (no extra gate — pre-FC is deterministic and not
        consulted, the lifecycle states are not advice phases).

        Args:
            phase: The agent phase being evaluated.

        Returns:
            The post-FC minimum-dwell seconds for ``phase``, or ``None`` when the
            phase has no extra cadence gate.
        """
        if phase is RoastPhase.DEVELOPMENT:
            return self._config.post_fc_min_consult_interval_seconds
        return None

    def note_call(
        self,
        *,
        phase: RoastPhase,
        telemetry: RoastTelemetry | None,
        now: float,
    ) -> None:
        """Record that the advisory step ran for a fired trigger: advance the
        baselines the next :meth:`evaluate` measures change against. Called
        once per triggered tick — including the no-telemetry skip path, where
        the advisor itself is not reached — so the interval restarts from the
        moment advice was attempted. Telemetry-derived baselines update only
        when telemetry is present, so a skip or a manual consult with no
        reading does not blank the delta baselines."""
        self._last_call_monotonic = now
        self._last_phase = phase
        if telemetry is not None:
            self._last_bean_temp_c = telemetry.bean_temp_c
            self._last_bean_ror_c_per_min = telemetry.bean_ror_c_per_min


@dataclass(frozen=True)
class ControllerSnapshot:
    """An atomic read of the controller's post-tick state (E9 wiring seam).

    The runner reads exactly one of these after each ``tick()`` and builds
    both the per-tick ``telemetry`` SSE frame and the persisted telemetry row
    from it — one read, no inter-field skew between phase, commanded levels,
    and the tick's telemetry. ``telemetry`` is the reading the tick consumed
    (``None`` when no session/read this tick); ``current_heat``/``current_fan``
    are the levels the controller has commanded (heat stays 0 after a restart
    until separately commanded — the restart invariant).

    ``development_elapsed_seconds`` (seconds since first crack) and
    ``development_percent`` (DTR — that duration as a share of the
    charge-referenced roast clock, #220) are read-only projections of the
    already-computed clocks the advisor reasons on; both ``None`` before first
    crack. The runner copies them onto the per-tick telemetry SSE frame so the
    operator sees the live development time + DTR (no client-side derivation).

    ``charge_elapsed_seconds`` is the operator-facing roast clock: seconds since
    charge (T0), ``None`` before charge, frozen at the drop value in cooling
    (#308). It is the charge-referenced projection of
    :meth:`RoastController._charge_elapsed_seconds`, surfaced so the SPA can
    render ROAST TIME with 0:00 = charge and re-origin the chart x-axis to charge
    (the header re-origin, part 1 = backend). It is **distinct** from
    ``roast_elapsed_seconds`` (serve/run-referenced — the chart's raw x lead-in,
    kept so the SPA can still draw the pre-charge preheat curve)."""

    phase: RoastPhase
    current_heat: int
    current_fan: int
    roast_elapsed_seconds: float
    charge_elapsed_seconds: float | None
    development_elapsed_seconds: float | None
    development_percent: float | None
    #: The resolved D96 feature flag for this run. This is surfaced separately
    #: from ``post_fc_heat_authority_state`` because the dormant loop can report
    #: ``HOLDING`` even when recovery authority is disabled; operators must be
    #: able to distinguish OFF from ARMED-but-not-recovering.
    post_fc_recovery_enabled: bool
    #: Authoritative three-way D96 heat-authority state from the most recently
    #: accepted post-FC control output in the current DEVELOPMENT dwell.
    post_fc_heat_authority_state: PostFcHeatAuthorityState | None
    post_fc_ror_setpoint_c_per_min: float | None
    post_fc_smoothed_ror_c_per_min: float | None
    post_fc_effective_heat_ceiling_percent: int | None
    telemetry: RoastTelemetry | None
    advisory_paused: bool
    #: Whether the charge/T0 clock has been stamped (``_charge_monotonic`` set,
    #: #235). The runner persists the absolute charge instant once this first
    #: reads ``True`` so a later restart→resume can restore the DTR clock. A
    #: pure read; advisory/display-only and never safety-gating.
    charge_detected: bool
    #: The post-FC output accepted or kept by the controller during this tick,
    #: retained even when a later same-tick drop/fault clears current
    #: DEVELOPMENT authority.
    #: Persistence uses this historical witness; live SSE uses the phase-gated
    #: current-authority fields above.
    accepted_post_fc_output: PostFcControlOutput | None = None


class RoastController:
    """Code-owned deterministic state machine and tick pipeline.

    Each tick (orchestration plan § Controller Loop): read MCP state →
    persist snapshot → evaluate safety → act/transition → (advisory if
    due, timeout-bounded) → validate → execute approved commands →
    persist results → emit events. Safety evaluation always happens
    before advisory calls and before command execution; a slow or failed
    advisor never blocks the tick.
    """

    def __init__(
        self,
        *,
        config: ControllerConfig,
        safety: SafetyPolicy,
        state_reader: StateReader,
        command_executor: CommandExecutor,
        snapshot_sink: SnapshotSink,
        event_emitter: EventEmitter,
        advisor: RoastAdvisor | None = None,
        clock: Clock = time.monotonic,
        reference_roast: ReferenceRoast | None = None,
    ) -> None:
        self._config = config
        self._safety = safety
        self._reader = state_reader
        self._executor = command_executor
        self._snapshots = snapshot_sink
        self._events = event_emitter
        self._advisor = advisor
        self._clock = clock
        # #567 Slice B: a DIFFERENT completed, well-rated roast of THIS SAME
        # bean, retrieved ONCE by the caller (RoastService, fail-soft, flag-
        # gated) before construction and cached here for the run's entire
        # lifetime — never re-retrieved per tick, and never mutated after
        # __init__. ``None`` (the default) reproduces today's behaviour
        # byte-for-byte: every existing caller/test that does not pass this
        # argument stays valid. See :meth:`_build_advisor_context`, the only
        # place this is read.
        self._reference_roast = reference_roast
        self._phase: RoastPhase = RoastPhase.IDLE
        self._profile: RoastProfile | None = None
        self._run_started_monotonic: float | None = None
        # Monotonic time of the first-crack transition into DEVELOPMENT, set in
        # ``transition_to`` and cleared on a new run/preheat. Drives
        # ``development_elapsed_seconds`` in the advisor context — the DTR clock
        # the advisor reasons about near the drop.
        self._first_crack_monotonic: float | None = None
        # Monotonic instant of the debounced T0/charge transition into
        # ROASTING_PRE_FIRST_CRACK (#219), set in ``_apply_phase_rules`` and
        # cleared on a new run/preheat. Stamps the charge clock for the advisor's
        # ``seconds_since_charge`` context and the charge-referenced DTR clock.
        self._charge_monotonic: float | None = None
        # T0-clock latches (#174), captured across the debounce streak so the
        # charge clock origins on the FIRST detect, not the later debounced
        # transition (which lands ~``t0_debounce_ticks`` late). ``_t0_first_detect_
        # monotonic`` is the clock at the streak's first detect tick; ``_pending_t0_
        # backdate`` latches the MCP turning-point delta (#337) whenever it first
        # appears during the streak (the ``beans_added`` event can race in a tick
        # after ``t0_status`` flips, so reading it only at the transition tick
        # missed it — one roast stamped T0 at bean 150 °C, ~10 s past the 179 °C
        # peak). Both reset on a broken streak / new run.
        self._t0_first_detect_monotonic: float | None = None
        self._pending_t0_backdate: float | None = None
        # Monotonic instant of the drop (the transition into COOLING), set in
        # ``transition_to`` and cleared on a new run/preheat (#239). Freezes the
        # development + charge clocks — and so the derived DTR (#220) — at their
        # drop values: post-drop the readout must hold the drop figure, not keep
        # climbing into cooling. Advisory/display-only; clamps no control path.
        self._drop_monotonic: float | None = None
        # Pending FC backdating delta (#337), in seconds, handed to the
        # ROASTING_PRE_FIRST_CRACK -> DEVELOPMENT stamp by ``_apply_phase_rules``
        # the tick the MCP-detected first crack fires. ``transition_to`` is a
        # generic method with no telemetry in scope, so the delta is staged here
        # and consumed-then-cleared at the stamp. v0.1.7 backdates the FC event to
        # the crack onset; subtracting this MCP-domain delta from the agent's
        # receive-tick clock anchors the development origin ~17 s earlier (the lag
        # the server corrected), shifting dev%/DTR/curve and the trim FC-ETA. None
        # for a manual/override FC or a pre-0.1.7 payload (stamp at receive-tick).
        self._pending_first_crack_backdate: float | None = None
        self._consecutive_read_failures = 0
        # D30 (#166): consecutive advisor *availability* failures
        # (provider_error / timeout). Incremented in _record_advisor_failure on
        # those statuses only, reset to 0 on a successful ``ok`` decision and on
        # run/recovery lifecycle edges. At max_consecutive_advisor_failures the
        # controller fails closed into operator_recovery_required. malformed /
        # unsafe (provider-reachable) never touch this counter.
        self._consecutive_advisor_failures = 0
        self._advisory_requested = False
        self._advisory_policy = AdvisoryCallPolicy(config)
        self._current_heat = 0
        self._current_fan = 0
        # Per-process roast counter for the v0.1.9 recording filename (#176).
        # The MCP filename only needs origin + an incrementing number; a simple
        # monotonic per-controller-lifetime counter (incremented at each
        # start_run) gives that without coupling the start path to the store.
        # Best-effort and advisory-only: it never touches control or safety.
        self._recording_roast_num = 0
        # Fail-closed retry latch (#206 / PR review, blocker finding). The
        # heat-off / safe-fan write is applied ONCE on entry into a terminal HOLD
        # phase. If that write (or an e-stop) fails TRANSIENTLY, the roaster may
        # still be hot — so the pending safe target is held here and re-attempted
        # on EACH subsequent latched tick (heat-off is monotonically toward
        # safety) until it confirms, at which point this clears and the latch goes
        # fully silent. This preserves the fail-closed guarantee under a flaky MCP
        # while still killing the re-read / re-eval / re-emit noise loop the latch
        # exists to fix. ``None`` = posture confirmed / nothing pending.
        self._pending_fail_safe: SafetyEvaluation | None = None
        # The terminal verdict the controller is currently LATCHED on (None when
        # not in a terminal phase). Drives the upward-only escalation in the
        # terminal-phase tick branch: re-evaluation acts only on a STRICTLY more
        # severe verdict (FAULT → EMERGENCY_STOP), so auto-escalation to the
        # hardware e-stop survives the anti-spam latch while a same/lesser verdict
        # never re-emits or re-fires (safety-reviewer carry-forward on #206).
        self._latched_verdict: SafetyVerdict | None = None
        # D35 §4-A / D40.5 (#276): the direction of each lever's last EXECUTED
        # post-FC move, fed into the coherence/deadband gate so a sub-threshold
        # direction reversal (the #218 30<->40<->30 thrash) is damped to a hold.
        # Reset on each new run/preheat (a fresh roast has no prior trajectory).
        # Context for the deterministic gate only — it never bypasses safety.
        self._heat_direction = LeverDirection.NONE
        self._fan_direction = LeverDirection.NONE
        # D35 §3 (#327): the anticipatory late-Maillard heat-trim LATCH. Set once
        # the trim window first opens this pre-FC phase (a clean FC-ETA + bean
        # floor), then held so the trim stays engaged through a noisy-ETA bounce —
        # the hysteresis that stops the deterministic heat oscillating
        # 100↔trim↔100 (the #218 lever-thrash). Reset on each new run/preheat; the
        # trim ends naturally when FC moves the phase out of pre-FC.
        self._trim_latched = False
        # Adaptive-depth damping STATE (#412): the last depth the controller
        # committed to the roaster after deadband + slew damping. ``None`` means
        # "no depth applied yet this pre-FC phase" — the first tick commits
        # unconditionally (no history to compare against). Reset alongside
        # ``_trim_latched`` so a new run / phase entry always starts fresh.
        self._trim_depth_applied: int | None = None
        # D82/D83 (#405 Slice B2): the deterministic post-FC RoR-target PI loop.
        # Constructed unconditionally (cheap, pure, side-effect free) but only
        # ever consulted/actuated when ``config.post_first_crack_control.enabled``
        # is True — see ``_apply_deterministic_post_fc_levers``. Flag OFF leaves
        # this instance permanently untouched: today's advisor-driven post-FC
        # regime is byte-for-byte unchanged.
        self._post_fc_controller = PostFcRorController(config.post_first_crack_control)
        # Monotonic time of the last post-FC loop ACTUATION (an accepted
        # ALLOW/CLAMP write), or ``None`` before the first one this DEVELOPMENT
        # engagement. Reset at the FC->DEVELOPMENT handoff (bumpless transfer)
        # and on every new run/preheat, mirroring ``_trim_depth_applied``'s reset
        # discipline. Advanced ONLY on an accepted write (#412 told==enforced
        # rule extended to a stateful loop) — a REJECTed tick must not consume
        # cadence budget.
        self._post_fc_last_actuation_monotonic: float | None = None
        # D96 slice 2 (#559): the MOST RECENT PostFcControlOutput the loop
        # computed this tick (or ``None`` before the loop has ever computed
        # one this engagement/process) — stashed here, in
        # ``_apply_deterministic_post_fc_levers``, so ``_build_advisor_context``
        # (which runs LATER in the same ``tick()``, see that method's call
        # order) can copy the setpoint/heat-authority-state fields VERBATIM
        # into ``AdvisorContext`` (told == enforced, the #497 precedent
        # applied to these two new fields) rather than recomputing an
        # equivalent value that could drift out of sync with what the loop
        # itself actually used to build the safety box THIS tick. Cleared
        # (set back to ``None``) whenever the loop disengages (mirrors
        # ``_post_fc_engaged``'s own reset), so a context built in a
        # different DEVELOPMENT dwell or an operator-resume (loop inert)
        # never reads a stale prior engagement's output.
        self._last_post_fc_output: PostFcControlOutput | None = None
        # Historical witness for the one accepted/kept post-FC output this tick.
        # It is reset at tick entry, set only where the output stands (executed
        # or already held), and deliberately survives a later same-tick phase
        # transition so the runner can retain a recovery entry that immediately
        # ends in a drop.
        self._accepted_post_fc_output: PostFcControlOutput | None = None
        # Safety-review fix (post-B2, Opus finding, MEDIUM): whether the post-FC
        # PI loop is ENGAGED for the current DEVELOPMENT dwell. ``DEVELOPMENT``
        # is reachable by two distinct edges — the true first-crack transition
        # (``ROASTING_PRE_FIRST_CRACK -> DEVELOPMENT``, where the loop is
        # bumpless-handoff-seeded from the real actuated pre-FC heat) AND an
        # operator resume out of recovery (``operator_recovery_required ->
        # DEVELOPMENT``, where NO seeding happens — the loop's integrator/EMA
        # would otherwise still hold whatever state a prior engagement left it
        # in, or the ``__init__`` default zero state after a cross-process
        # restart). Gating only on ``phase is DEVELOPMENT`` (the original B2
        # guard) could not tell these two edges apart, so a restart ->
        # recovery -> operator-resume sequence could engage the loop from a
        # PHANTOM (non-bumpless) PI state — a heat command disconnected from
        # the roaster's real level. This flag makes that distinction explicit:
        # ``True`` iff the CURRENT DEVELOPMENT dwell was entered via the true
        # FC edge; set ``True`` only at that edge (``transition_to``) and
        # cleared on every other transition (mirroring how ``_trim_latched``
        # tracks a narrower per-phase engagement). When ``False`` — e.g. the
        # operator-resume edge — ``_apply_deterministic_post_fc_levers`` stays
        # fully inert and the advisor resumes driving post-FC heat/fan, exactly
        # the pre-B2 fallback behaviour (see ``_run_advisory``'s
        # ``post_fc_loop_active`` gate).
        self._post_fc_engaged: bool = False
        # #732: latched for the life of THIS controller (one per run), so the
        # ambient-decline warning fires once rather than at 1 Hz. Deliberately
        # NOT cleared on transition, unlike its neighbours above — the point is
        # one line per run, not one per phase.
        self._ambient_decline_warned: bool = False
        # D96 slice 1.5 (#561), Codex round-1 finding #3: latched TRUE the
        # moment ANY drop-failure clamp fires (see
        # ``_clamp_heat_after_failed_drop``), cleared unconditionally on
        # every ``transition_to`` (the identical per-DEVELOPMENT-dwell
        # discipline ``_post_fc_engaged``/``_last_post_fc_output`` already
        # follow) — never carried into a later dwell. While set, the post-FC
        # loop's own raise-suppression (``_apply_deterministic_post_fc_
        # levers``) treats EVERY tick as if it were drop-eligible for the
        # purpose of suppressing a raise, regardless of this tick's OWN
        # eligibility and regardless of ``heat_authority_state`` (see that
        # method's own comment for why: forcing the recovery state machine
        # fully back to ``HOLDING`` on a successful clamp — the #412
        # told==enforced fix for the ceiling/reality gap — also clears the
        # `heat_authority_state is not HOLDING` signal the pre-existing
        # same-tick suppression depends on, and a persistent RoR shortfall
        # can re-confirm entry the very next tick; without this latch that
        # re-confirmation would re-raise heat while the SAME drop keeps
        # failing, on ticks the pre-existing mirrors do not even cover — the
        # advisor's own ``should_drop`` path has no drop-eligibility mirror
        # here at all, by design, since pre-suppressing on advisor-drop
        # legality would neuter recovery through most of development).
        self._post_fc_raise_suppressed_after_clamp: bool = False
        # D157: one-way release of the ambient-conditioned DEVELOPMENT fan
        # destination ceiling. The live RoR conjunction can arm it, but once
        # armed no later RoR is consulted for this purpose. Cleared on every
        # phase transition, making it strictly per-DEVELOPMENT-dwell state.
        self._post_fc_fan_ceiling_released: bool = False
        # D157: whether the ambient-conditioned ceiling has bound at least
        # once in this DEVELOPMENT dwell. If any later signal would stop the
        # ceiling binding, the release latch above arms so the told box cannot
        # oscillate back to a narrower ceiling within the same dwell.
        self._post_fc_fan_ceiling_engaged_once: bool = False
        # D156/D157 scoring observability: each transition logs once per dwell,
        # even though the predicate is evaluated on every tick and consult.
        self._post_fc_fan_ceiling_engage_logged: bool = False
        self._post_fc_fan_ceiling_release_logged: bool = False
        # #498 (D89 Tier 1, safety-reviewer BLOCKER-1 fix): the advisor's
        # safety-evaluated fan TARGET in loop mode — a held desire, never an
        # actuated value. The advisor consult and the taper's own write both
        # run every tick (taper first, per ``tick()``'s documented order);
        # letting each issue its OWN ``set_targets`` collided on
        # ``min_seconds_between_commands`` (both default 5 s cadences landing
        # in the SAME tick is the common case), so the taper's heat-moving
        # write consumed the tick's one rate-limit slot and the advisor's fan
        # was REJECTed almost every time it mattered. The fix coalesces to
        # ONE writer: ``_run_advisory`` safety-evaluates the advisor's fan
        # (bounds-only, ``seconds_since_last_command=None`` — this is a
        # target computation, not a roaster write, so it never consumes or is
        # gated by the write cadence) and stores the CLAMPED value here;
        # ``_apply_deterministic_post_fc_levers`` reads it back and issues the
        # single per-interval ``set_targets(taper_heat, desired_fan)``, so
        # exactly one write (and one rate-limit slot) exists per tick in loop
        # mode. ``None`` until the advisor's first loop-mode consult sets it
        # (the taper holds fan at ``self._current_fan`` until then — no
        # change from the pre-#498 startup behaviour). Cleared on every phase
        # transition (mirroring ``_post_fc_engaged``) and re-armed to ``None``
        # at the FC edge (mirroring ``_post_fc_last_actuation_monotonic``) so
        # a later engagement never inherits a stale desired fan from an
        # earlier one (D88 C2 discipline, restart-never-auto-resumes intact —
        # this is a target value, never itself a write).
        self._post_fc_desired_fan_percent: int | None = None
        # The advisor's own pre-clamp fan request when, and only when, D156's
        # narrowed destination ceiling caused the clamp. Retained so D157 can
        # restore that actor-authored request when the box releases, rather
        # than leaving the last brake rationed at the obsolete ceiling.
        self._post_fc_doctrine_clamped_fan_request_percent: int | None = None
        self._last_command_monotonic: float | None = None
        self._t0_streak = 0
        self._t0_confirmed = False
        self._guidance_emitted = False
        # One-way latch for the pre-FC drying-end signal (#351): set the tick the
        # bean probe first crosses ``drying_end_bean_temp_c`` after the turning
        # point, so the DRYING_END event/marker fires exactly once and never
        # re-arms within a run. Reset on each new run/preheat. Observability only.
        self._drying_end_emitted = False
        # #409: one-way witness latch set when `_arm_pre_fc_milestones` observes a
        # negative bean RoR after charge. Required before the `turning_point` SSE
        # event may fire: without a prior negative sample the first ≥0 RoR reading
        # after charge would be a false landmark (no dip actually observed). Reset
        # on each new run/preheat alongside `_drying_end_emitted`.
        self._seen_negative_ror_after_charge = False
        self._operator_state_entered: float | None = None
        self._operator_timeout_alerted = False
        # #332: set once the operator has acknowledged the fault (the runner's
        # teardown signal, mirrored here). While set, the latched-fault tick skips
        # the upward-escalation re-read — the run is finalising this tick, heat is
        # already off, and a wedged-child read there would only delay the
        # acknowledge from clearing (the roast-3 "slow to clear" latency). Cleared
        # on a new run/preheat. Never re-enables heat or weakens the heat-off retry.
        self._fault_acknowledged = False
        # E9: the telemetry the most recent tick consumed (for the runner's
        # post-tick snapshot — SSE telemetry frame + persisted row), and the
        # operator advisory pause latch (pause/resume_advisory, D19).
        self._last_telemetry: RoastTelemetry | None = None
        self._advisory_paused = False
        # D40.3 / D40.5 (#275): the per-tick control-loop CONTEXT accumulator —
        # the roast-so-far curve (bounded recent full-res window + milestone
        # summary) and the model's own recommendation trace (#218). Context
        # assembly only: it never actuates hardware, never evaluates safety, and
        # holds no control authority. Reset on each new run/preheat. Wiring its
        # payload into the live post-FC consult is #276; this story builds it.
        self._history = RoastHistory(
            curve_window_samples=config.curve_window_samples,
            decision_trace_entries=config.decision_trace_entries,
        )

    # --- E4-S1: transitions ---

    @property
    def phase(self) -> RoastPhase:
        """Current agent phase."""
        return self._phase

    def snapshot(self) -> ControllerSnapshot:
        """An atomic read of the controller's current state (E9 wiring seam).

        The runner reads exactly one of these after each ``tick()`` to build
        the per-tick telemetry frame and persisted row — see
        :class:`ControllerSnapshot`. Pure: no side effects, no clock advance
        beyond reading elapsed."""
        # ``_last_post_fc_output`` is already cleared whenever the loop
        # disengages, but phase-gating here is deliberate defence-in-depth:
        # diagnostics from a completed DEVELOPMENT dwell must never appear as
        # current authority during cooling or a terminal/recovery phase.
        post_fc_output = (
            self._last_post_fc_output if self._phase is RoastPhase.DEVELOPMENT else None
        )
        return ControllerSnapshot(
            phase=self._phase,
            current_heat=self._current_heat,
            current_fan=self._current_fan,
            roast_elapsed_seconds=self._roast_elapsed_seconds(),
            charge_elapsed_seconds=self._charge_elapsed_seconds_or_none(),
            development_elapsed_seconds=self._development_elapsed_seconds(),
            development_percent=self._development_percent(),
            post_fc_recovery_enabled=(self._config.post_first_crack_control.recovery_enabled),
            post_fc_heat_authority_state=(
                None if post_fc_output is None else post_fc_output.heat_authority_state
            ),
            post_fc_ror_setpoint_c_per_min=(
                None if post_fc_output is None else post_fc_output.setpoint_c_per_min
            ),
            post_fc_smoothed_ror_c_per_min=(
                None if post_fc_output is None else post_fc_output.smoothed_ror_c_per_min
            ),
            post_fc_effective_heat_ceiling_percent=(
                None if post_fc_output is None else post_fc_output.effective_ceiling_percent
            ),
            telemetry=self._last_telemetry,
            advisory_paused=self._advisory_paused,
            charge_detected=self._charge_monotonic is not None,
            accepted_post_fc_output=self._accepted_post_fc_output,
        )

    def _roast_elapsed_seconds(self) -> float:
        """Seconds since run/preheat start (``start_run``), or ``0.0`` if no run.

        The monotonic run clock that feeds :class:`ControllerSnapshot` and so the
        SSE/persisted telemetry the SPA renders (the chart x-value + the operator
        ROAST TIME readout). Deliberately **run/preheat-referenced**: the dashboard
        plots each point at ``t = elapsed_seconds`` and must keep showing the
        pre-charge preheat/RoR curve (#165) — a charge-referenced value would
        collapse every pre-charge row onto ``x=0``. Re-origining the chart at charge
        (Artisan-style 0:00) is a deliberate UX change held for #220, not #219.

        For the advisor's DTR clock (charge-referenced, #219) see
        :meth:`_charge_elapsed_seconds`.
        """
        if self._run_started_monotonic is None:
            return 0.0
        return self._clock() - self._run_started_monotonic

    def _charge_elapsed_seconds(self) -> float:
        """Charge-referenced roast clock: seconds since charge (T0), ``0.0``
        before charge (#219).

        Referenced to the debounced charge/T0 instant (``_charge_monotonic``,
        the same instant ``seconds_since_charge`` and the post-charge settle
        window use), **not** run/preheat start. This is the advisor's DTR
        denominator only — DTR = ``development_elapsed / charge_elapsed`` — which
        the v4 prompt and the bake-off fixtures define charge-referenced; it has
        no meaning before there is a bean on the drum. It feeds
        :attr:`AdvisorContext.roast_elapsed_seconds` and nothing the SPA renders;
        the chart/readout clock is the run-referenced :meth:`_roast_elapsed_seconds`.
        """
        if self._charge_monotonic is None:
            return 0.0
        return self._effective_now() - self._charge_monotonic

    def _charge_elapsed_seconds_or_none(self) -> float | None:
        """The charge-referenced roast clock for the SPA, ``None`` before charge.

        The operator-facing ROAST TIME source surfaced on
        :class:`ControllerSnapshot` (#308): seconds since charge (T0), ``None``
        before charge so the SPA can show '—' until the bean is on the drum
        (rather than a misleading ``0:00`` during preheat), and frozen at the
        drop value in cooling (via :meth:`_effective_now`). It is the same clock
        as :meth:`_charge_elapsed_seconds` (which returns ``0.0`` pre-charge for
        the advisor's DTR denominator), differing only in the pre-charge sentinel
        the display contract wants.

        Distinct from :meth:`_roast_elapsed_seconds` (serve/run-referenced — the
        chart's raw x lead-in). Advisory/display-only: it bounds no control,
        transition, verdict, executor, or drop gate.
        """
        if self._charge_monotonic is None:
            return None
        return self._charge_elapsed_seconds()

    def _effective_now(self) -> float:
        """The clock instant the elapsed-time readouts reference, frozen at drop.

        Returns ``self._clock()`` during the live roast, but once a drop is
        recorded (``_drop_monotonic`` set on the COOLING transition, #239) it
        returns the drop instant instead. Both elapsed clocks
        (:meth:`_charge_elapsed_seconds` and :meth:`_development_elapsed_seconds`)
        reference it, so the development time, the charge-referenced roast clock,
        and the derived DTR (#220) all freeze together at their drop values
        rather than climbing into cooling. ``min`` clamps post-drop reads to the
        drop instant — it never reports a time after the drop.

        Mostly advisory/display, with ONE control coupling to be honest about
        (#337): the DTR these clocks produce flows through
        :meth:`_development_percent` into the #313 advisor-drop-coherence guard
        (:meth:`_drop_development_is_coherent` →
        :meth:`SafetyPolicy.evaluate_advisor_drop_coherence`), which REJECTs vs
        allows an advisor ``should_drop``. So these clocks do bound that one gate —
        but it fails safe: a guard REJECT is a HELD drop (no roaster write), and the
        guard owns only the advisor drop path, never the operator manual drop, the
        safety box, e-stop, or any phase transition. No verdict/executor/transition
        is otherwise driven by these clocks.
        """
        now = self._clock()
        if self._drop_monotonic is None:
            return now
        return min(now, self._drop_monotonic)

    def _backdated_now(self, backdate_seconds: float | None) -> float:
        """Anchor a milestone clock at the MCP-reported backdated instant (#337).

        The MCP (a separate child process, D6) reports the T0 turning-point / FC
        crack-onset by backdating its event timestamp, and carries the *delta* to
        the confirmation tick in the event payload (``confirmed_at − onset``,
        surfaced on ``RoastTelemetry``). Because cross-process ``time.monotonic``
        clocks are **not** comparable, the agent never assigns the MCP's absolute
        timestamp; instead it subtracts that in-domain delta from its own
        receive-tick clock. The agent receives the event at ≈ the confirmation
        moment, so ``self._clock() - delta`` reconstructs the onset in the agent's
        own clock domain.

        Fail-safe: a missing delta (manual mark / pre-0.1.7 payload — ``None``) or
        a non-finite / negative one falls back to ``self._clock()`` (stamp at
        receive-tick, the pre-backdating behaviour). The result is therefore never
        in the future and never garbage — it is ``<= self._clock()`` by
        construction (the future-rejection contract holds: a backdated milestone
        is in the past).

        This anchors the charge / development clocks the advisor + SPA read, and
        through :meth:`_development_percent` the DTR these clocks produce feeds the
        #313 advisor-drop-coherence guard (#337) — so honouring the backdate raises
        the system dev% (~+1.8 pp) and releases the advisor drop ~1-2 pp earlier
        (on a truer dev%). That coupling fails safe: the guard only ever HOLDS a
        drop (no roaster write), gates only the advisor drop path, and drives no
        phase transition, verdict, executor, operator drop, safety box, or e-stop.
        The #327 deterministic trim is unaffected — its FC-ETA is charge-clock
        independent (bean-temp/RoR projection).

        Args:
            backdate_seconds: The MCP-domain backdating delta in seconds, or
                ``None`` to stamp at the current receive-tick.

        Returns:
            The backdated monotonic instant in the agent's clock domain.
        """
        now = self._clock()
        if backdate_seconds is None or not math.isfinite(backdate_seconds):
            return now
        if backdate_seconds < 0.0:
            return now  # never fabricate a future-referenced milestone clock
        return now - backdate_seconds

    def _reset_t0_debounce(self) -> None:
        """Reset the T0 debounce streak and the charge-clock latches (#174).

        A broken streak — an absent/faulted read, or a tick without MCP-reported
        T0 — discards the candidate charge instant; the next streak re-latches its
        own first detect + turning-point delta.
        """
        self._t0_streak = 0
        self._t0_first_detect_monotonic = None
        self._pending_t0_backdate = None

    def _charge_origin_monotonic(
        self, first_detect: float | None, backdate_seconds: float | None
    ) -> float:
        """Charge-clock origin: the first debounced-T0 detect, backdated to onset (#174).

        Anchors the charge clock at ``first_detect`` — the instant the MCP first
        reported T0 (the start of the debounce streak), NOT the later debounced
        transition tick that lands ~``t0_debounce_ticks`` late — then subtracts the
        MCP turning-point backdating delta (#337, confirmed − turning point) to
        reach the local-max bean temp before the decline (the true charge / dip
        onset). Without both corrections T0 stamped ~10 s late (one roast: bean
        150 °C, past the 179 °C peak).

        Defensive: falls back to the live clock if ``first_detect`` is absent (the
        streak reaching its threshold always sets it). Never future-referenced —
        ``first_detect`` ≤ now, and only a finite non-negative delta moves it
        earlier (a None / non-finite / negative delta is ignored, as in
        :meth:`_backdated_now`).

        Args:
            first_detect: Monotonic instant of the streak's first detect, or None.
            backdate_seconds: The MCP turning-point backdating delta, or None.

        Returns:
            The backdated charge-clock origin in the agent's monotonic domain.
        """
        base = first_detect if first_detect is not None else self._clock()
        if (
            backdate_seconds is None
            or not math.isfinite(backdate_seconds)
            or backdate_seconds < 0.0
        ):
            return base
        return base - backdate_seconds

    def _development_elapsed_seconds(self) -> float | None:
        """Seconds since first crack, frozen at drop, or ``None`` before FC.

        The development clock the advisor reasons about near the drop. The DTR
        the advisor computes is ``development_elapsed / charge_elapsed`` (the
        charge-referenced roast clock, :meth:`_charge_elapsed_seconds`). ``None``
        until the first-crack transition arms ``_first_crack_monotonic`` in
        :meth:`transition_to`; once a drop is recorded it freezes at
        ``drop_monotonic - first_crack_monotonic`` (#239) via
        :meth:`_effective_now` — the post-drop readout holds the drop figure
        instead of counting into cooling.
        """
        if self._first_crack_monotonic is None:
            return None
        return self._effective_now() - self._first_crack_monotonic

    def _development_percent(self) -> float | None:
        """DTR (development time ratio) as a percentage of the WHOLE roast (#220).

        ``development_elapsed / charge_elapsed * 100`` — the SAME ratio the
        advisor computes (``development_elapsed_seconds /
        roast_elapsed_seconds`` in :class:`AdvisorContext`), so the operator's
        readout and the advisor's DTR agree. Charge-referenced
        (:meth:`_charge_elapsed_seconds`, #219), NOT the run/serve clock.
        ``None`` before first crack (no development yet) and guarded against a
        zero/negative charge clock (FC can only follow charge, but stay defensive
        so a clock edge never divides by zero). A pure read of already-computed
        clocks: it commands nothing and changes no transition/verdict.
        """
        development_elapsed = self._development_elapsed_seconds()
        if development_elapsed is None:
            return None
        charge_elapsed = self._charge_elapsed_seconds()
        if charge_elapsed <= 0.0:
            return None
        return development_elapsed / charge_elapsed * 100.0

    def _drop_development_is_coherent(self, system_percent: float | None) -> bool:
        """Whether the SYSTEM's development supports honouring an advisor drop (#312).

        The deterministic half of the trustworthy-drop fix: a drop is irreversible,
        so the controller will only honour an advisor ``should_drop=true`` when the
        *system's own* development percent (:meth:`_development_percent`,
        charge/FC-referenced — never the model's claimed number) has reached the
        target window. Coherent when

            ``development_percent >= target_development_percent - drop_dev_margin_percent``

        with ``drop_dev_margin_percent`` the small named tolerance from
        :class:`~roastpilot_agent.config.ControllerConfig` (default 3 pp), so a drop
        a percentage point or two short of target still goes through while a drop
        materially short (the fabricated-"we're done" failure) is blocked.

        Fails OPEN (returns ``True``) only when the guard cannot be evaluated —
        there is no profile target, or development has not started so
        ``system_percent`` is ``None`` (the safety drop evaluation still owns the
        phase boundary in that case). It commands nothing, gates only the advisor
        drop path, and never touches the operator manual-drop path, the safety box,
        or e-stop.

        The development percent is **passed in** (computed once by the caller, the
        #294 compute-once pattern) so the value that decides the block is exactly
        the value the rejection note and the persisted :class:`SafetyEvaluation`
        report — no sub-tick recompute can let them drift apart.

        Args:
            system_percent: The SYSTEM's development percent from a single
                :meth:`_development_percent` read this tick, or ``None`` (no
                profile / pre-FC) — both fail open.

        Returns:
            ``True`` to allow the advisor drop to proceed to safety evaluation,
            ``False`` to block it (the system's development is below the window).
        """
        if self._profile is None:
            return True
        if system_percent is None:
            return True
        floor = self._profile.target_development_percent - self._config.drop_dev_margin_percent
        return system_percent >= floor

    def can_transition(self, target: RoastPhase) -> bool:
        """Whether ``target`` is a legal next phase from the current one."""
        if target is self._phase:
            return False
        if target in UNIVERSAL_TARGETS:
            return True
        return target in TRANSITION_TABLE[self._phase]

    def transition_to(self, target: RoastPhase) -> None:
        """Commit a phase transition or raise :class:`InvalidTransitionError`."""
        if not self.can_transition(target):
            raise InvalidTransitionError(self._phase, target)
        previous = self._phase
        self._phase = target
        # Safety-review fix (post-B2, Opus finding, MEDIUM): ``_post_fc_engaged``
        # is True IFF the current DEVELOPMENT dwell was entered via the TRUE
        # first-crack edge (``ROASTING_PRE_FIRST_CRACK -> DEVELOPMENT``) — set
        # unconditionally here, on EVERY transition, so it can never carry a
        # stale True from a previous DEVELOPMENT engagement into a later one
        # reached by a different edge (e.g. an operator resume out of
        # recovery). Set True only in the FC-edge branch below (alongside the
        # bumpless-handoff seed); every other transition — including
        # DEVELOPMENT -> COOLING/FAULTED/OPERATOR_RECOVERY_REQUIRED and an
        # ``operator_recovery_required -> DEVELOPMENT`` resume — lands here
        # first and clears it. This is deliberately unconditional (not scoped
        # to "only when leaving DEVELOPMENT") so the flag's truth is a pure
        # function of "was THIS transition the FC edge", never of history.
        self._post_fc_engaged = False
        # #498: the held desired-fan target is per-engagement state exactly
        # like ``_post_fc_engaged`` above — clear it unconditionally on every
        # transition so a later DEVELOPMENT dwell (a fresh FC edge, or an
        # operator resume where the loop stays inert and the advisor drives
        # both levers directly) never inherits a stale desired fan from an
        # earlier engagement.
        self._post_fc_desired_fan_percent = None
        self._post_fc_doctrine_clamped_fan_request_percent = None
        # D96 slice 2 (#559): the stashed last-computed post-FC output is
        # likewise per-engagement state — clear it unconditionally on every
        # transition (same discipline as the two fields immediately above)
        # so ``_build_advisor_context`` never reads a prior engagement's
        # setpoint/heat-authority-state into a context built for a different
        # DEVELOPMENT dwell or an operator-resume where the loop stays inert.
        self._last_post_fc_output = None
        # D96 slice 1.5 (#561), Codex round-1 finding #3: the post-clamp
        # raise-suppression latch is likewise per-DEVELOPMENT-dwell state —
        # clear it unconditionally on every transition (identical discipline)
        # so a fresh dwell (a new FC edge, or an operator resume) never
        # inherits a stale "keep suppressing" latch from an earlier one. A
        # SUCCESSFUL drop naturally clears it this same way (transition_to
        # COOLING), which is exactly the intended "latch persists until the
        # drop that keeps failing finally succeeds, or the dwell ends"
        # behaviour.
        self._post_fc_raise_suppressed_after_clamp = False
        # D157: the fan-ceiling release latch is likewise per-DEVELOPMENT-dwell
        # state. Clear it unconditionally on every transition so a later dwell
        # never inherits free-fan authority from an earlier one.
        self._post_fc_fan_ceiling_released = False
        self._post_fc_fan_ceiling_engaged_once = False
        self._post_fc_fan_ceiling_engage_logged = False
        self._post_fc_fan_ceiling_release_logged = False
        if previous in TERMINAL_LATCH_PHASES and target not in TERMINAL_LATCH_PHASES:
            # Leaving a terminal HOLD phase is an EXPLICIT operator action
            # (acknowledge → idle, resume, start cooling): the operator now owns
            # the levers, so any unconfirmed fail-safe retry latch is dropped — it
            # must not keep forcing heat-off into a run the operator just resumed
            # or a cooling cycle they just started (#206 / PR review). The latched
            # verdict clears too, so a fresh terminal entry later starts its
            # escalation tracking from scratch.
            self._pending_fail_safe = None
            self._latched_verdict = None
        if target in (RoastPhase.STARTING, RoastPhase.PREHEATING):
            # Per-run latches reset (T0 confirmation, debounce streak,
            # add-beans guidance) on a new run AND on every preheating
            # entry: a recovery-resume into preheating declares "back
            # before charge", so the pre-T0 overrun guard must re-arm —
            # a stale _t0_confirmed would disarm it (safety review, E4-S3).
            self._t0_streak = 0
            self._t0_confirmed = False
            self._guidance_emitted = False
            # A new run/preheat re-arms the one-way drying-end latch (#351) so the
            # next roast can emit its own DRYING_END signal. Observability only.
            self._drying_end_emitted = False
            # A new run/preheat re-arms the negative-RoR witness (#409) so a fresh
            # roast can detect its own turning-point dip. Cleared here alongside
            # _drying_end_emitted — both are per-run observability latches.
            self._seen_negative_ror_after_charge = False
            # A new run/preheat clears the fault-acknowledged teardown flag (#332):
            # a fresh roast has no acknowledged fault, so the escalation re-read is
            # fully armed again.
            self._fault_acknowledged = False
            # A new run/preheat resets the development clock; it is (re)armed
            # only on the first-crack transition below.
            self._first_crack_monotonic = None
            # A new run/preheat clears any staged FC backdating delta (#337):
            # belt-and-braces with the consume-on-stamp clear, so a delta from a
            # prior roast can never anchor a fresh roast's development clock.
            self._pending_first_crack_backdate = None
            # A new run/preheat is "back before charge": clear the charge clock
            # so ``seconds_since_charge`` is None (#219). It is restamped at the
            # debounced T0 transition.
            self._charge_monotonic = None
            # Clear the T0-clock latches (#174) so a prior roast's streak can never
            # origin a fresh charge clock.
            self._t0_first_detect_monotonic = None
            self._pending_t0_backdate = None
            # A new run/preheat un-freezes the elapsed clocks (#239): clear the
            # drop instant so the next roast's development time and DTR run live
            # again rather than staying frozen at the prior roast's drop value.
            self._drop_monotonic = None
            # A new run/preheat starts a fresh per-tick context history (#275):
            # the roast-so-far curve, milestones, and decision trace are
            # per-roast. Context only; clears no control state.
            self._history.reset()
            # A new run/preheat clears the post-FC lever trajectory (#276): the
            # coherence gate has no prior direction to reverse on the first
            # post-FC move of the new roast.
            self._heat_direction = LeverDirection.NONE
            self._fan_direction = LeverDirection.NONE
            # A new run/preheat clears the anticipatory trim latch (#327) and
            # the adaptive-depth damping state (#412): a fresh roast has not
            # yet opened the late-Maillard window, so both must re-arm from
            # scratch — never inherit a prior roast's latch or applied depth.
            self._trim_latched = False
            self._trim_depth_applied = None
            # A new run/preheat clears the post-FC PI loop's cadence timer
            # (#405 Slice B2): a fresh roast has not yet reached first crack, so
            # there is no prior actuation to pace against. The loop's internal
            # integrator/EMA state is re-seeded at the FC->DEVELOPMENT handoff
            # below via ``reset(initial_heat_percent=...)``, not here — this
            # phase never re-enters DEVELOPMENT directly.
            self._post_fc_last_actuation_monotonic = None
        if target is RoastPhase.ROASTING_PRE_FIRST_CRACK:
            # Clear the trim latch on EVERY entry into pre-FC, not just the
            # new-run/preheat path above (#327, safety-reviewer low on the latch
            # PR). The transition table allows a same-process
            # ``operator_recovery_required -> roasting_pre_first_crack`` resume that
            # BYPASSES preheating, so a fault/recovery mid-pre-FC then resume would
            # otherwise carry a STALE latch — the next tick would trim a now-cooler
            # bean (below the 155 °C floor) where a fresh window would never open,
            # weakening the §8.4 "FC still arrives" floor guarantee. Resetting on
            # entry is harmless on the normal preheating->pre-FC path (the latch is
            # already False there) and the latch re-arms correctly the moment the
            # window next opens. (Cross-process restart is already safe: a fresh
            # controller defaults the flag False; this is only the in-process resume
            # edge. The restart-never-auto-resumes invariant is untouched — this
            # clears a flag, it does not actuate heat/fan.)
            self._trim_latched = False
            # Reset damping state (#412) alongside the latch so the depth
            # history from a prior pre-FC entry never anchors the deadband on
            # a recovery resume.
            self._trim_depth_applied = None
        if target is RoastPhase.COOLING and self._drop_monotonic is None:
            # The drop instant (#239): every drop path lands in COOLING (the
            # advisor drop, the operator drop, and the pre-FC early-abort drop),
            # so freezing here covers them all. Stamp once — guard against a
            # re-entry never restamping a later instant. From here the
            # development + charge clocks (and the derived DTR) hold their drop
            # values instead of climbing into cooling. Advisory/display-only:
            # bounds no transition, verdict, executor, or drop gate.
            self._drop_monotonic = self._clock()
        if previous is RoastPhase.ROASTING_PRE_FIRST_CRACK and target is RoastPhase.DEVELOPMENT:
            # Arm the development clock only on the true first-crack edge — both
            # FC paths (MCP detection and the operator override) cross it. A
            # recovery resume into development (OPERATOR_RECOVERY_REQUIRED →
            # DEVELOPMENT) is NOT a fresh FC: it must not restamp the clock to
            # now, or an already-developed run would read elapsed≈0. On such a
            # resume the in-memory FC time is preserved (same process) or stays
            # None (after a restart) — advisory-only either way (safety review).
            #
            # #337: when the MCP backdated the FC event (v0.1.7), origin the
            # development clock on the crack ONSET, not this receive-tick — apply
            # the staged MCP-domain delta to the agent's own clock. The stage is
            # consumed (set to None) so it never leaks into a later, unrelated
            # FC-edge transition (e.g. a recovery resume). A manual/override FC or
            # a pre-0.1.7 payload leaves the stage None ⇒ stamp at receive-tick.
            self._first_crack_monotonic = self._backdated_now(self._pending_first_crack_backdate)
            self._pending_first_crack_backdate = None
            # Record the first-crack milestone for the per-tick context summary
            # (#275): the development clock's origin and a curve landmark the
            # post-FC loop reasons from. Context only; the last reading the tick
            # consumed supplies the bean temperature at the crack.
            if self._last_telemetry is not None:
                self._history.record_milestone(
                    RoastMilestone(
                        kind=RoastMilestoneKind.FIRST_CRACK,
                        elapsed_since_charge_seconds=self._charge_elapsed_seconds(),
                        bean_temp_c=self._last_telemetry.bean_temp_c,
                    )
                )
            # D82/D88 (#405 Slice B2): bumpless handoff for the deterministic
            # post-FC RoR-taper PI loop. Seed it from the ACTUATED pre-FC heat
            # (``self._current_heat`` — the last commanded value, never an
            # unbounded target) so a zero-error first compute reproduces exactly
            # that level: no heat dip or jump at the tick the loop takes over
            # (see the PostFcRorController.reset docstring for the one case
            # where this is not exact). The taper's r0 anchors to the RoR THIS
            # SAME tick measured (D88) — the true FC edge always has a fresh
            # telemetry reading (it is the reading that fired the transition),
            # but its RoR can still legitimately be unavailable (e.g. not
            # enough history yet) or read a degenerate low/negative value (a
            # post-charge-crash FC); either way the loop's own r0 clamp floors
            # at ``taper_end_ror_c_per_min``, so feeding that same value
            # through when a RoR sample is unavailable is not a separate
            # special case — it lands on the identical floor the clamp would
            # apply to a genuinely low reading.
            # This branch is the TRUE first-crack edge ONLY — it never fires on
            # an ``operator_recovery_required -> development`` resume, which is
            # a distinct transition and lands on the unconditional
            # ``self._post_fc_engaged = False`` above instead. Constructed and
            # reset unconditionally (cheap, pure): whether it is ever actually
            # consulted is gated on the ``enabled`` flag (and, per the fix
            # below, ``_post_fc_engaged``) in ``_apply_deterministic_post_fc_levers``.
            ror_at_engagement = (
                self._last_telemetry.bean_ror_c_per_min
                if self._last_telemetry is not None
                and self._last_telemetry.bean_ror_c_per_min is not None
                else self._config.post_first_crack_control.taper_end_ror_c_per_min
            )
            self._post_fc_controller.reset(
                initial_heat_percent=self._current_heat,
                ror_at_engagement_c_per_min=ror_at_engagement,
            )
            # Reset the cadence timer too, so the very first DEVELOPMENT control
            # tick actuates immediately rather than waiting a full
            # ``control_interval_seconds`` after an arbitrary FC instant.
            self._post_fc_last_actuation_monotonic = None
            # Safety-review fix (post-B2, Opus finding, MEDIUM): ENGAGE the loop
            # only for a DEVELOPMENT dwell reached via this true FC edge. A
            # restart -> recovery -> operator-resume sequence also reaches
            # DEVELOPMENT (``operator_recovery_required -> DEVELOPMENT``, the
            # ``operator_resume`` path) but does NOT run this branch (its
            # ``previous`` is ``OPERATOR_RECOVERY_REQUIRED``, not
            # ``ROASTING_PRE_FIRST_CRACK``) — so it inherits ``False`` from the
            # unconditional clear above, and the loop's phase guard alone
            # (``phase is DEVELOPMENT``) is NOT sufficient to prevent it from
            # actuating there from a PHANTOM (non-bumpless-seeded) PI state.
            # ``_post_fc_engaged`` closes that gap: gating
            # ``_apply_deterministic_post_fc_levers`` on it (in addition to
            # ``phase is DEVELOPMENT``) keeps the loop inert on the resume
            # edge, and the advisor resumes driving post-FC heat/fan there
            # instead (the pre-B2 fallback — see ``_run_advisory``'s
            # ``post_fc_loop_active`` gate), so post-FC heat control is never
            # silently absent after a resume.
            self._post_fc_engaged = True
        if target in UNIVERSAL_TARGETS:
            # D16 operator-timeout tracking starts on entering a true
            # operator-required state — never in normal phases.
            self._operator_state_entered = self._clock()
            self._operator_timeout_alerted = False
        else:
            self._operator_state_entered = None
        self._events.emit(RoastEventKind.PHASE_CHANGED, {"phase": target.value})

    # --- E4-S2: tick pipeline ---

    def request_advisory(self) -> None:
        """Operator/manual advisory trigger.

        Sets the manual flag the next tick honours unconditionally — it
        bypasses the change-based :class:`AdvisoryCallPolicy` gates,
        including phase scoping, so an explicit operator request always
        reaches :meth:`_run_advisory` (where the command×phase matrix has
        the final say on whether the resulting advice can apply)."""
        self._advisory_requested = True

    async def tick(self) -> None:
        """Run one controller tick in the documented order.

        A controller already latched into a terminal HOLD phase
        (:data:`TERMINAL_LATCH_PHASES`) runs a REDUCED tick: it re-attempts an
        unconfirmed fail-safe write, then re-reads + re-evaluates ONLY to allow
        an upward-only escalation (a ``faulted`` run whose still-live MCP reports
        a hard-ceiling breach auto-escalates to the hardware emergency stop). It
        NEVER re-emits the same-or-lesser verdict, and a failed re-read (dead MCP)
        holds silently — so the post-#206 "infinite error loop" (the identical
        FAULT re-emitted every tick) stays fixed while the automatic upward
        escalation the controller had before the latch is preserved. Operator
        recovery actions (acknowledge / resume / cooling / emergency-stop) are
        separate, always-available methods, so the latch never reduces what the
        operator can do (#206 operable-faulted intact).
        """
        # Tick-scoped historical witness. Operator actions are drained before
        # this method, so clearing here also prevents a prior tick's accepted
        # output leaking through a later operator drop/recovery transition.
        self._accepted_post_fc_output = None
        if self._phase in TERMINAL_LATCH_PHASES:
            # If the fail-safe write did not confirm on entry (a transient MCP
            # write failure), re-attempt the heat-off write here — the latch must
            # never strand the roaster hot. Once it confirms, this is silent.
            await self._retry_pending_fail_safe()
            # Upward-only re-evaluation: escalate to a STRICTLY more severe verdict
            # (FAULT → EMERGENCY_STOP) if the live MCP now reports one; otherwise
            # hold without re-emitting anything (and hold silently if the MCP is
            # dead). This is the only place a latched tick may fire new hardware.
            await self._maybe_escalate_while_latched()
            self._check_operator_timeout()
            # A stale advisory request must not survive the latch into a later
            # resumed/cooling tick (mirrors the fail-closed branch below).
            self._advisory_requested = False
            return
        telemetry, read_failed = await self._read_telemetry()
        # Remember the reading this tick consumed so the runner's post-tick
        # snapshot publishes the same telemetry it acted on (E9).
        self._last_telemetry = telemetry
        await self._snapshots.persist_snapshot(telemetry)
        evaluation = self._evaluate_safety(telemetry, read_failed=read_failed)
        await self._snapshots.persist_evaluation(evaluation)
        if await self._act_on_safety(evaluation):
            # Fail-closed action taken: no advisory, no commands — and a
            # stale advisory request must not survive into a later tick
            # (it would otherwise fire in faulted/recovery).
            self._advisory_requested = False
            return
        await self._apply_phase_rules(telemetry)
        self._check_operator_timeout()
        # D35 §3 (#222): pre-FC the controller deterministically sets heat/fan
        # from the policy — runs AFTER _apply_phase_rules so a tick that just
        # charged (PREHEATING → ROASTING_PRE_FIRST_CRACK) actuates the pre-FC
        # lever, and a tick that just hit FC does NOT (it falls to the advisor).
        # The advisory step below is a no-op in these phases (gated out).
        await self._apply_deterministic_pre_fc_levers(telemetry)
        # D82/D83 (#405 Slice B2): the deterministic post-FC RoR-target PI
        # loop, called immediately AFTER the pre-FC lever step for the same
        # reason the pre-FC comment documents its own ordering — a tick that
        # JUST hit first crack this tick (ROASTING_PRE_FIRST_CRACK →
        # DEVELOPMENT, via ``_apply_phase_rules`` above) must not double-
        # actuate: ``_apply_deterministic_pre_fc_levers`` no-ops the moment the
        # phase is DEVELOPMENT (no deterministic target there), and this call
        # picks the loop up in the SAME tick the phase flips. No-op entirely
        # unless ``post_first_crack_control.enabled`` is True (default False —
        # today's advisor-driven post-FC path is unaffected).
        await self._apply_deterministic_post_fc_levers(telemetry)
        # D88 amendment A1 (#405): the decoupled ceiling-guard drop, called
        # BEFORE the dev%/temp anchor below. Ordering decision: the guard is
        # the HARDER of the two safety bounds (a run where the loop keeps
        # chasing heat could cross the bean-temp leg alone while dev% still
        # lags — the ratifier's rationale for why the guard must be able to
        # force the drop independently of the dev% leg), so it gets first
        # refusal each tick. No-op entirely unless
        # ``post_first_crack_control.ceiling_guard_drop_enabled`` is True
        # (default False — today's fully advisor-driven drop is unaffected) —
        # deliberately its OWN flag, unconnected to the RoR-taper loop's
        # ``enabled``/``_post_fc_engaged`` bundle the anchor below uses.
        await self._maybe_ceiling_guard_drop(telemetry)
        # D84 (#405 Slice C): the deterministic drop anchor, called immediately
        # AFTER the post-FC RoR loop step and the ceiling guard above, and
        # BEFORE the advisory consult below. Ordering decision: this must run
        # after the loop's heat/fan write (a drop that fires this tick should
        # do so using the freshest possible bean-temp/dev% read, and firing
        # after the loop step means a tick that both actuates a heat trim AND
        # crosses the drop threshold still drops the same tick rather than
        # waiting one more). It must run BEFORE ``_maybe_run_advisory`` so
        # that once either deterministic drop path fires and transitions the
        # phase to COOLING, the SAME tick's advisory consult below is a clean
        # no-op: ``_maybe_run_advisory`` only consults in the advisory phases
        # (COOLING is not one), so there is no risk of the advisor ALSO
        # recommending/executing a second drop_beans() the same tick — the
        # guard, the anchor, and the advisor drop path can never fire more
        # than once between them in one tick (the guard runs first each tick,
        # so if IT fires, this call's own phase-is-DEVELOPMENT gate below
        # already sees COOLING and no-ops). No-op entirely unless
        # ``post_first_crack_control.enabled`` is True (default False —
        # today's fully advisor-driven drop is unaffected).
        await self._maybe_deterministic_drop(telemetry)
        # D40.3 (#275): accumulate the roast-so-far curve + milestones for the
        # per-tick control-loop context AFTER the phase rules + deterministic
        # pre-FC levers have run, so the sample captures the charge tick itself
        # and pairs the reading with the heat/fan the controller actually
        # commanded this tick (the (action, response) history the model reads).
        # Context assembly only — it actuates nothing and evaluates no safety; a
        # fail-closed tick returns above and records no sample (a faulting roast
        # is not building context for an advisor that will not be consulted).
        self._record_curve_history(telemetry)
        # D157: evaluate the one-way fan-ceiling release after every
        # deterministic actuation and drop path, immediately before advisory.
        # This catches a heat-floor dip between consults; after a drop the phase
        # is already COOLING and the pure builder returns ``None``.
        self._arm_post_fc_fan_release(self._post_fc_fan_signal(telemetry))
        await self._maybe_run_advisory(telemetry)

    async def _maybe_run_advisory(self, telemetry: RoastTelemetry | None) -> None:
        """Consult the call-frequency policy and run the advisory step when it
        fires. No advisor wired or no active run ⇒ nothing to consult; the
        manual flag is cleared either way so it never survives into a later
        tick. Gating on the profile here keeps a stray pre-run manual request
        from advancing the policy's interval baseline before the advisor is
        even reachable."""
        manual_request = self._advisory_requested
        self._advisory_requested = False
        if self._advisor is None or self._profile is None:
            return
        if self._phase in _DETERMINISTIC_PRE_FC_PHASES:
            # D35 §3 (#222): pre-FC is deterministic — the advisor is NOT
            # consulted before first crack, not even on a manual request (cleared
            # above). The controller already actuated the deterministic lever this
            # tick (_apply_deterministic_pre_fc_levers); there is no advice to add.
            return
        if self._advisory_paused:
            # Operator paused advice (D19). The controller keeps running every
            # other tick rule — safety, phase transitions, the queue drain —
            # only the advisory consult is suppressed. Clearing the manual flag
            # above means a request raised while paused does not survive resume.
            return
        trigger = self._advisory_policy.evaluate(
            phase=self._phase,
            telemetry=telemetry,
            now=self._clock(),
            manual_request=manual_request,
        )
        if trigger is None:
            return
        await self._run_advisory(telemetry, trigger)
        self._advisory_policy.note_call(phase=self._phase, telemetry=telemetry, now=self._clock())

    def _check_operator_timeout(self) -> None:
        """D16: alert (once) when a true operator-required state has waited
        past ``operator_timeout_seconds``. Never applies in normal phases —
        the machine is hardware-off in these states, so this nags, it does
        not actuate."""
        if self._phase not in UNIVERSAL_TARGETS or self._operator_state_entered is None:
            return
        if self._operator_timeout_alerted:
            return
        waited = self._clock() - self._operator_state_entered
        if waited > self._config.operator_timeout_seconds:
            self._operator_timeout_alerted = True
            self._events.emit(
                RoastEventKind.SAFETY_ALERT,
                {
                    "alert": "operator_timeout",
                    "phase": self._phase.value,
                    "waited_seconds": waited,
                },
            )

    async def _apply_deterministic_pre_fc_levers(self, telemetry: RoastTelemetry | None) -> None:
        """Deterministically drive heat/fan from the policy before FC (D35 §3, #222).

        The highest-stakes path in the D35 chain: it actuates real heat/fan on the
        roaster before first crack. Every write still passes the existing safety
        path — the command×phase matrix gate (``evaluate_command_phase``) then
        ``evaluate_command`` with the phase's single-source control box — so the
        deterministic levers are safety-gated exactly like advisor output (no new
        verdict, no invariant change).

        Behaviour:

        * **Only the two deterministic pre-FC phases** (preheat, charge→FC) carry
          a lever target (``PhaseControlLimits.has_deterministic_target``); every
          other phase returns immediately. A restart/recovery NEVER lands in these
          phases without explicit operator action (``recover_from_restart`` enters
          ``operator_recovery_required``), so this never auto-resumes heat/fan — it
          actuates only during a normally-progressing run.
        * **No active run** (no profile) ⇒ no-op: the policy/box is meaningless
          before a run is loaded.
        * **Idempotent**: it writes only when the current heat/fan differ from the
          target, so after the first pre-FC write each later tick is a no-op — no
          rate-limit churn, no redundant serial writes. The flat-floor target is
          steady high heat / low fan; the heat floor == the active target, so even
          a spurious lower value is clamped back up (the #218 70→40→20→0 crash is
          structurally impossible pre-FC).
        * **Anticipatory heat trim (#327)**: in the late-Maillard → FC window
          (keyed on the live bean temperature + the #229 predicted-FC ETA via
          :meth:`_trim_signal`) the policy lowers the deterministic heat target to
          a moderate trim level so the env cools and RoR bends into FC before the
          drop ceiling. The trim NEVER raises heat above the floor and fails closed
          to the flat floor whenever the FC-ETA is unknown / the window is shut —
          the floor stays the always-on guarantee FC still arrives (§8.4).

        Args:
            telemetry: The reading this tick consumed. Its bean temperature keys
                the trim window's bean-temp guard (the FC-ETA is derived from the
                accumulated curve); ``None`` (a failed read) leaves the window
                keyed on the last curve sample.
        """
        if self._profile is None:
            return
        # Build the live trim signal (bean-temp + FC-ETA + the per-run latch), arm
        # the latch the moment THIS tick's fresh-engage window first opens, then
        # resolve the box from the (possibly freshly-latched) signal. The latch is
        # the hysteresis: once the window has opened it stays engaged through a
        # noisy-ETA bounce, so the deterministic heat does not oscillate
        # 100↔trim↔100 (the #218 lever-thrash). ``trim_window_open`` ignores the
        # carried latch, so a garbage ETA never arms it; the latch resets per
        # run/preheat in ``transition_to``.
        trim_signal = self._arm_trim_latch(self._trim_signal(telemetry))
        box = self._control_limits(trim_signal=trim_signal)
        if not box.has_deterministic_target:
            return
        # Validated all-or-nothing on PhaseControlLimits: a deterministic target
        # implies both lever values are present.
        assert box.heat_target_percent is not None  # noqa: S101 (validator invariant)
        assert box.fan_target_percent is not None  # noqa: S101 (validator invariant)
        target_heat = box.heat_target_percent
        target_fan = box.fan_target_percent
        # Adaptive-depth damping (#412): when the adaptive path is active (flag
        # ON and trim engaged this tick), smooth the resolved depth through a
        # deadband + slew filter so raw RoR noise doesn't oscillate the trim
        # heat every tick.  The non-adaptive path (``adaptive_depth_enabled``
        # False, or trim not yet latched) skips this entirely — byte-for-byte
        # the proven roast-6 behaviour.
        trim_cfg = self._config.pre_first_crack_levers.late_maillard_trim
        damping_active = trim_cfg.adaptive_depth_enabled and self._trim_latched
        if damping_active:
            # _damp_trim_depth is PURE: it returns the candidate WITHOUT mutating
            # _trim_depth_applied.  State is advanced below, only after an
            # ACCEPTED write, so rate-limited REJECT ticks do not consume slew
            # budget (#412 Fix 2).
            damped_heat = self._damp_trim_depth(target_heat, trim_cfg)
            target_heat = damped_heat
            # Rebuild the control box with floor==target==damped_heat (#412 Fix 1).
            # The policy-built box has heat_floor_percent==raw undamped depth, so
            # passing it to evaluate_command would CLAMP a slew-UP step back up to
            # the raw floor, bypassing the slew limit on the heat-UP leg.  Setting
            # the floor to damped_heat is safe because damped_heat ∈ [min_trim,
            # max_trim] ⊆ [0, ceiling], so the floor stays within the original
            # safety ceiling and we never raise the floor above a prior actuated
            # value on a downward ramp (slew may hold damped_heat above raw_depth
            # during ramp-down, but it is always ≤ the last accepted write — the
            # anchor — and therefore ≤ the ceiling).  The original ceiling is kept.
            box = box.model_copy(
                update={
                    "heat_floor_percent": damped_heat,
                    "heat_target_percent": damped_heat,
                }
            )
        if self._current_heat == target_heat and self._current_fan == target_fan:
            # Already at the deterministic target — no write (avoids rate-limit
            # churn and redundant serial writes; the target is constant pre-FC).
            return
        # Matrix gate first (SET_HEAT must be valid in this phase — it is, for both
        # pre-FC phases — but the gate stays the single source so this never drifts
        # from safety), then the bounds/rate-limit clamp.
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.SET_HEAT, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:  # pragma: no cover — both
            # pre-FC phases are in the SET_HEAT matrix row; unreachable defensive guard.
            return
        evaluation = self._safety.evaluate_command(
            requested_heat=target_heat,
            requested_fan=target_fan,
            seconds_since_last_command=self._seconds_since_last_command(),
            # The SAME single-source box (#273/#222): the gate clamps to exactly
            # the phase box the target was resolved from — told == enforced for the
            # deterministic path too. The target sits inside the box by construction
            # (PhaseControlLimits validates it), so a clean run yields ALLOW.
            bounds=box,
        )
        await self._snapshots.persist_evaluation(evaluation)
        executed = await self._execute_targets(evaluation)
        # Advance damping state only after an ACTUALLY EXECUTED write (#412 Fix
        # 2; sharpened by the Codex actuator-failure finding, #405 Slice B2
        # fix round 2): key on ``executed`` — ``_execute_targets``'s own report
        # of whether the write reached the roaster — NOT on
        # ``evaluation.verdict`` alone. A REJECT (rate-limited) tick never
        # even attempts the write, so it was already correctly excluded by the
        # old verdict check; but an ALLOW/CLAMP verdict whose ``set_targets``
        # call then raises (a transient actuator/serial failure) is NOT an
        # executed command either — the old verdict-only check would have
        # advanced ``_trim_depth_applied`` there anyway (a phantom advance:
        # the roaster's real heat is unchanged, but the anchor moved as if it
        # had). ``executed`` is exactly "did the roaster's real heat/fan
        # change to match this evaluation", so gating on it folds the REJECT
        # case and the actuator-failure case into one correct check.
        if damping_active and executed:
            self._trim_depth_applied = evaluation.adjusted_heat

    async def _apply_deterministic_post_fc_levers(self, telemetry: RoastTelemetry | None) -> None:
        """Deterministically hold a target RoR post-FC via the PI loop (D82/D83, #405 Slice B2).

        Gated end-to-end on ``self._config.post_first_crack_control.enabled``
        (default ``False``): when the flag is off this method is a pure no-op on
        every path, so the flag-off behaviour is byte-for-byte identical to
        before this slice — the advisor's ``target_heat``/``target_fan`` keep
        actuating post-FC exactly as they do today (``_run_advisory`` /
        ``_execute_advisor_levers``).

        **#498 (D89 Tier 1, safety-reviewer BLOCKER-1 fix): the SOLE writer in
        loop mode.** Fan is the advisor's lever here (revising D88(5)'s pinned
        fan), but the advisor's own consult (``_run_advisory``) never writes
        directly — it only safety-evaluates its fan request and holds the
        clamped result in ``self._post_fc_desired_fan_percent`` (a target, no
        actuation). THIS method is the only place ``set_targets`` is called in
        loop mode, applying ``(this tick's computed heat, the held desired
        fan)`` together in ONE write — so exactly one write, and one
        ``min_seconds_between_commands`` rate-limit slot, is consumed per tick.
        Two independent writers previously collided there: both this loop's
        cadence and the advisor's default consult cadence are 5 s, so a
        same-tick collision was the common case, and the loop (which runs
        FIRST in ``tick()``'s documented order) would consume the tick's one
        rate-limit slot, REJECTing the advisor's fan write almost every time
        heat also moved.

        **Invariants held (unchanged by this method):**

        * Every write this method issues passes the SAME safety path as every
          other roaster write — the command×phase matrix
          (``evaluate_command_phase``) then ``evaluate_command`` with a single-
          source :class:`~roastpilot_agent.control_policy.PhaseControlLimits`
          box (told == enforced, #273/#412) — so CLAMP/REJECT are honoured
          exactly as they are for the advisor and the pre-FC lever.
        * The 196 °C bitter ceiling and the emergency-drop bound are NOT
          reasoned about here at all — they live in ``SafetyPolicy``'s
          temperature rules (``_evaluate_safety``), which run earlier in
          ``tick()`` and already fail-closed (heat off) independently of
          whether this loop is engaged. This method can only ever narrow the
          DEVELOPMENT heat/fan box the gate already enforces; it cannot loosen
          it.
        * Emergency stop remains reachable from every phase (unaffected: this
          method issues SET_HEAT/SET_FAN only, never touches the e-stop path).
        * A restart never auto-resumes this loop: ``recover_from_restart``
          always lands a possibly-active run in ``operator_recovery_required``,
          never directly in ``DEVELOPMENT``. But ``DEVELOPMENT`` itself is
          reachable by TWO distinct edges — the true FC edge
          (``ROASTING_PRE_FIRST_CRACK -> DEVELOPMENT``, bumpless-handoff seeded)
          and an operator resume out of recovery
          (``operator_recovery_required -> DEVELOPMENT``, NOT seeded) — and
          ``phase is DEVELOPMENT`` alone cannot distinguish them. This method
          therefore ALSO gates on ``self._post_fc_engaged`` (safety-review fix,
          post-B2), which ``transition_to`` sets ``True`` only on the true FC
          edge and clears on every other transition (including the resume
          edge): the loop can actuate only in a DEVELOPMENT dwell reached via a
          normally-progressing FC, never from the phantom (non-bumpless) PI
          state a resume would otherwise expose it to. On a resume the advisor
          resumes driving post-FC heat/fan instead (see ``_run_advisory``'s
          ``post_fc_loop_active`` gate) — post-FC control is never silently
          absent.
        * Temperatures stay Celsius throughout (RoR is °C/min); the advisor
          never receives MCP write tools (unchanged — this method has no
          advisor in its call graph at all).

        **Fail-closed on missing RoR:** if telemetry is absent or carries no
        ``bean_ror_c_per_min`` this tick, the loop does NOT actuate — heat is
        left exactly where it is. A stale/undamped PI output computed from a
        RoR the caller cannot trust would violate the #412 told==enforced
        rule at its source, so the safest choice is simply not to compute at
        all rather than compute and then decide not to use the result.

        **Cadence:** the loop actuates roughly every
        ``control_interval_seconds`` (default 5 s, matching the post-FC
        advisory cadence), not every 1 s tick — RoR is a derivative signal and
        chasing it every tick would fight thermocouple noise (the same
        reasoning ``ror_smoothing_alpha`` encodes inside the loop itself).

        **The #412 told==enforced rule, extended to a stateful loop:** the
        safety box's heat floor/target is built from the loop's ACTUATED
        output (never an unbounded pre-clamp value), and the loop's internal
        PI state (integrator, EMA, bias) advances ONLY when
        :meth:`_execute_targets` reports the write ACTUALLY REACHED the
        roaster (its ``bool`` return) — never on ``evaluation.verdict`` alone.
        Two distinct cases mean "the write did not land", and both are folded
        into that one check (fix round 2, a Codex finding): a REJECTed safety
        verdict (e.g. a rate-limited tick, or the defensive phase-matrix
        branch) never even attempts ``set_targets``; a TRANSIENT ACTUATOR
        FAILURE is an ALLOW/CLAMP verdict whose ``set_targets`` call then
        raises — an outcome the verdict alone cannot distinguish from success,
        but ``_execute_targets``'s ``False`` return catches it. In BOTH cases
        the roaster's real heat/fan is unchanged from before this tick. A
        snapshot is taken via
        :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.snapshot_state`
        immediately before :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.compute`;
        when the write did not land that snapshot is restored, so the
        tentative step is fully undone and the NEXT accepted-and-executed
        write is computed from the same pre-step state — exactly the
        discipline ``_apply_deterministic_pre_fc_levers`` applies to
        ``_trim_depth_applied`` (#412 Fix 2, likewise keyed on
        :meth:`_execute_targets`'s return since fix round 2). The cadence
        timer (``_post_fc_last_actuation_monotonic``) is likewise NOT advanced
        when the write did not land, so the next tick retries at the same
        elapsed-time budget rather than losing a full cadence interval to a
        command that never reached the roaster. The one case that is
        DELIBERATELY NOT a restore — the idempotent "already at this target"
        no-write skip — is documented in-line at that branch below: it is
        treated as accepted because the roaster's real state already equals
        the computed output, so there is no state/reality gap to protect
        against, and restoring there would wrongly freeze the EMA/integrator
        on the loop's own steady-state (deadband-holding) case.

        Args:
            telemetry: The reading this tick consumed, or ``None`` on a
                failed/sessionless read (the loop does not actuate either way).
        """
        config = self._config.post_first_crack_control
        if (
            not config.enabled
            or self._profile is None
            or self._phase is not RoastPhase.DEVELOPMENT
            or not self._post_fc_engaged
            or telemetry is None
            or telemetry.bean_ror_c_per_min is None
        ):
            return
        now = self._clock()
        if self._post_fc_last_actuation_monotonic is not None:
            elapsed = now - self._post_fc_last_actuation_monotonic
            if elapsed < config.control_interval_seconds:
                return
            dt_seconds = elapsed
        else:
            # First DEVELOPMENT control tick since the bumpless-handoff reset
            # (``transition_to``): no prior actuation to measure elapsed time
            # against, so feed the loop its own configured cadence as a sane
            # dt rather than an arbitrary/zero value.
            dt_seconds = config.control_interval_seconds
        # D157/#498: resolve a release against the currently actuated heat
        # before selecting the fan for this cadence-eligible write. If fan is
        # now the only brake, the advisor's retained pre-clamp request can ride
        # this SAME coalesced, safety-evaluated taper write; the consult remains
        # target-only and never becomes a second writer. This guard provides
        # same-tick ordering at THIS pre-write site only; the unguarded
        # post-actuation arming in ``tick`` still runs later in the same tick.
        # If the taper early-returns — notably because bean RoR is missing — no
        # effective floor is stashed, so that later site treats the floor as
        # unknown, arms release for the dwell, and the ceiling never binds. That
        # is deliberate and matches the documented baseline/resume carve-out:
        # unknown brake state fails toward full #498 fan authority.
        if self._last_post_fc_output is not None:
            self._arm_post_fc_fan_release(self._post_fc_fan_signal(telemetry))
        # dt_seconds is always > 0 here: control_interval_seconds is validated
        # `gt=0` on the config model, and `elapsed` above is only used when it
        # already cleared the `>= control_interval_seconds` (itself > 0) gate.
        pre_compute_state = self._post_fc_controller.snapshot_state()
        output = self._post_fc_controller.compute(
            measured_ror_c_per_min=telemetry.bean_ror_c_per_min, dt_seconds=dt_seconds
        )
        # D96 (#559), PR #560 Codex findings (guard-eligible AND
        # deterministic-drop-eligible same-tick RAISES — round 1's P1 and
        # round 2's P2, the same class of bug for the two different drop
        # paths): this method runs BEFORE BOTH `_maybe_ceiling_guard_drop`
        # AND `_maybe_deterministic_drop` in `tick()`'s order (see that
        # ordering comment below), so — without this check — a tick where
        # EITHER drop path is already eligible while the recovery ceiling is
        # elevated would still WRITE a RAISED heat command to the roaster
        # before that drop fires a few lines later: the drop stops the
        # roast, but the raise still reached hardware on its way there
        # (worse, if `drop_beans()` itself then fails transiently, the roast
        # is left sitting in DEVELOPMENT with recovery-raised heat past the
        # drop's own target point — round 2's Codex finding). Skip THIS
        # tick's write — restoring the tentative `compute` step exactly like
        # a rejected write, so the loop's internal state is untouched —
        # whenever BOTH: (1) this step's tentative output would RAISE heat
        # ABOVE what is currently ACTUATED (`output.heat_percent >
        # self._current_heat` — the SAME `self._current_heat` source the
        # idempotence check below already trusts as "what the roaster
        # really holds"), and (2) the SAME tick is independently eligible
        # for EITHER drop path.
        #
        # **Round 4 fix (Codex P1): ADDS an actuated-level comparison
        # alongside `heat_authority_state`, rather than replacing it.** The
        # round-3 form skipped on `heat_authority_state is not HOLDING`
        # alone — but during the exit/glide tail `output.heat_percent` can be
        # BELOW the currently-actuated (still-raised) heat, i.e. the
        # tentative write is a *lowering* move. Skipping THAT write inverted
        # the mechanism's own intent: on a drop-eligible tick where
        # `drop_beans()` then fails (transiently), the round-3 form would
        # repeat the skip on every subsequent tick too, permanently freezing
        # the roaster at its raised heat in DEVELOPMENT past the drop's own
        # target — the exact "stuck at raised heat" failure this fix exists
        # to prevent. A hold-or-lower write on a drop-due tick is always SAFE
        # (it can only ever bring heat closer to, or hold below, the current
        # level) and is precisely what a stuck-at-raised roaster needs to
        # recover on its own even while the drop keeps failing — so only a
        # genuine RAISE (strictly greater than actuated) is suppressed.
        #
        # `heat_authority_state is not HOLDING` MUST stay as the first
        # condition (not dropped in favor of the actuated-level check alone):
        # D88's own never-add-heat-beyond-entry law can ALSO legitimately
        # raise heat above `self._current_heat` outside D96 entirely (e.g.
        # the anti-stall floor recovering FROM a 0% bumpless handoff up
        # toward `effective_ceiling` — a plain D88 write, recovery-inactive,
        # that must NEVER be suppressed by a D96-specific guard). Requiring
        # BOTH conditions scopes the skip to exactly "a D96 recovery/glide
        # raise, on a drop-eligible tick" — never an ordinary D88 climb.
        #
        # BOTH mirrors below must track their sources exactly — a future
        # change to either `_maybe_ceiling_guard_drop`'s or
        # `_maybe_deterministic_drop`'s own eligibility condition (e.g. a new
        # gating clause) needs the SAME change made here, or this skip can
        # silently drift out of sync with what actually makes a drop fire.
        guard_config = self._config.post_first_crack_control
        # Mirrors `_maybe_ceiling_guard_drop`'s own eligibility exactly (the
        # guard flag on, bean temperature already at or past the guard
        # line) — that method's own phase/telemetry-None guards are already
        # satisfied by this point (this method's own top-of-function gate
        # already required `self._phase is RoastPhase.DEVELOPMENT` and
        # `telemetry is not None`).
        guard_eligible_this_tick = (
            guard_config.ceiling_guard_drop_enabled
            and telemetry.bean_temp_c >= guard_config.ceiling_guard_temp_c
        )
        # Mirrors `_maybe_deterministic_drop`'s own eligibility exactly (bean
        # at/past `target_drop_temp_c` AND system dev% at/past
        # `target_development_percent`) — that method's own
        # ``enabled``/``_post_fc_engaged``/phase/``self._profile`` guards are
        # already satisfied here too (this method's own top-of-function gate
        # requires the identical `config.enabled`/`self._post_fc_engaged`/
        # phase/`self._profile is not None` bundle). ``system_dev_percent``
        # is ``None`` (development not yet computable) fails closed to
        # NOT-eligible here, mirroring that method's own fail-closed branch.
        system_dev_percent = self._development_percent()
        deterministic_drop_eligible_this_tick = (
            system_dev_percent is not None
            and telemetry.bean_temp_c >= self._profile.target_drop_temp_c
            and system_dev_percent >= self._profile.target_development_percent
        )
        recovery_ceiling_elevated = (
            output.heat_authority_state is not PostFcHeatAuthorityState.HOLDING
        )
        tentative_write_would_raise_heat = output.heat_percent > self._current_heat
        # D96 slice 1.5 (#561), Codex round-1 finding #3: the pre-existing
        # same-tick suppression above (elevated authority AND drop-eligible
        # THIS tick) is necessary but not sufficient once a clamp has fired
        # this dwell. `_clamp_heat_after_failed_drop`'s own `_force_recovery_
        # exit` resets the recovery state machine fully to HOLDING to close
        # the ceiling/reality gap (#412) — but that SAME reset clears
        # `recovery_ceiling_elevated`'s own signal, and a persisting RoR
        # shortfall can re-confirm entry the very next tick (a fresh
        # `recovery_confirm_ticks` run, which can be as short as 1 tick).
        # Without an independent latch, that re-confirmed entry would raise
        # heat again while the SAME drop keeps failing every tick — on ticks
        # the two eligibility mirrors above do not even cover, since the
        # ADVISOR's own `should_drop` path (the residual #561 exists for) has
        # no such mirror here at all (by design: pre-suppressing on advisor-
        # drop legality would neuter recovery through most of development).
        # `_post_fc_raise_suppressed_after_clamp` closes this gap: once ANY
        # clamp has fired this dwell, suppress every raise unconditionally
        # (no eligibility check needed — the advisor can attempt, and fail, a
        # drop on literally any tick) until the drop finally succeeds or the
        # dwell ends (both clear the latch via `transition_to`).
        # Codex round-2 finding #2 (PR #569): the suppression must be
        # HEAT-ONLY. It used to `restore_state` + `return` unconditionally —
        # discarding the tentative PI step (correct: the raise itself must
        # never land) but ALSO skipping the ENTIRE write, fan included. In
        # loop mode this method is the SOLE writer (#498) — the advisor's own
        # consult never actuates fan directly, it only stashes a target in
        # `self._post_fc_desired_fan_percent` — so blocking the whole write
        # here strands a legitimate, SAFE fan move alongside the unsafe heat
        # raise. That collides with the D96 doctrine the operator ratified
        # ("fan is valuable and should be used to control", #559): a fan
        # move is never the hazard here, only a heat raise is. The fix:
        # discard the tentative PI step exactly as before (the raise must
        # never be actuated, and the PI's internal state must not race ahead
        # of a heat command that will never land — the same #412 reasoning
        # the executed/not-executed branch below applies), but substitute a
        # HELD heat value (`self._current_heat` — never the raise) for the
        # rest of this method's box/write/idempotence logic, so a fan-only
        # move still reaches the roaster on a suppressed tick.
        heat_suppressed_this_tick = tentative_write_would_raise_heat and (
            self._post_fc_raise_suppressed_after_clamp
            or (
                recovery_ceiling_elevated
                and (guard_eligible_this_tick or deterministic_drop_eligible_this_tick)
            )
        )
        if heat_suppressed_this_tick:
            self._post_fc_controller.restore_state(pre_compute_state)
        actuated_heat = self._current_heat if heat_suppressed_this_tick else output.heat_percent
        # Build the DEVELOPMENT box from the ACTUATED PI output (#412
        # told==enforced): start from the full DEVELOPMENT box (heat/fan
        # 0-100, the profile-aware temperature ceilings), then narrow heat's
        # floor AND target to the loop's output — never an undamped/pre-clamp
        # value — OR, on a suppressed tick, to the HELD current heat (never
        # the discarded raise).
        #
        # #498 (D89 Tier 1, revises D88(5); coalesced to ONE writer per
        # BLOCKER-1, safety-reviewer): fan is NO LONGER pinned to a single
        # configured value, and this is now the SOLE write per tick in loop
        # mode — the advisor's own consult (``_run_advisory``'s loop-mode
        # branch) never writes; it only safety-evaluates its fan request and
        # holds the CLAMPED result in ``self._post_fc_desired_fan_percent``
        # (a target, no actuation). This write applies
        # ``(taper_heat, desired_fan)`` together — or, on a suppressed tick,
        # ``(held_heat, desired_fan)`` — the desired fan defaults to
        # ``self._current_fan`` (a genuine hold) until the advisor's first
        # loop-mode consult sets it, mirroring pre-#498 startup behaviour
        # exactly. Under D156/D157 the desired fan may already be the advisor
        # consult's CLAMPED destination (for example 70). This write deliberately
        # resolves an unnarrowed fan box because it actuates that already-checked
        # VALUE; re-narrowing against a ceiling that engaged after the consult
        # would silently cut fan with no actor requesting the move and make told
        # differ from enforced. Accepted residual: a warm-room consult's desired
        # fan (for example 90) may still land after the room turns cool; the next
        # consult clamps any new request, bounded by advisory cadence and failing
        # toward free #498 fan authority.
        desired_fan = (
            self._post_fc_desired_fan_percent
            if self._post_fc_desired_fan_percent is not None
            else self._current_fan
        )
        box = self._control_limits().model_copy(
            update={
                "heat_floor_percent": actuated_heat,
                "heat_target_percent": actuated_heat,
            }
        )
        if self._current_heat == actuated_heat and self._current_fan == desired_fan:
            # Already at the actuated heat target (the loop's own computed
            # value, OR — Codex round-2 finding #2 — the HELD current heat on
            # a suppressed tick) AND the desired fan: no MCP write is issued
            # (mirrors the pre-FC idempotence guard: avoids rate-limit churn
            # and redundant serial writes). Checking BOTH fields (not heat
            # alone) is the #498 coalesced-writer fix: a fan-only move (heat
            # idempotent, desired fan changed) must still fire — a fan move
            # is a real command, and this is the ONLY write path fan has in
            # loop mode. On a SUPPRESSED tick this branch is reached whenever
            # fan is ALSO already at its desired value — genuinely nothing
            # to do this tick (heat is correctly held, and fan needs no
            # correction either).
            #
            # DELIBERATE DECISION on the #412 "state advances only on accepted
            # write" rule for THIS idempotent case: on a NON-suppressed tick,
            # the PI state (integrator + EMA, already advanced by the
            # `compute()` call above) is KEPT, and the cadence timer DOES
            # advance — this counts as accepted, not rejected. Reasoning:
            # `self._current_heat`/`self._current_fan` ARE the last values
            # the roaster actually holds, and they already equal
            # `output.heat_percent`/`desired_fan` by this branch's own
            # condition — so there is no gap between "what was
            # computed/held" and "what the roaster is actually doing" for
            # the state to race ahead of. This is UNLIKE a REJECT (rate
            # limit) or a phase-matrix REJECT, where the roaster's real
            # state is UNCHANGED from before this tick while the
            # integrator/EMA would have advanced as if the new command had
            # taken effect — THAT mismatch is what the restore-on-reject
            # rule exists to prevent (a "phantom advance"). Restoring state
            # here instead would freeze the integrator and the RoR EMA every
            # steady-state (deadband-holding) tick — precisely the loop's
            # expected common case — breaking both the EMA's cross-tick
            # smoothing (#386/#412 lesson: it must keep tracking the live
            # RoR every cadence tick) and the integrator's ability to keep
            # accumulating a small deadband-adjacent error toward the next
            # real move. On a SUPPRESSED tick, by contrast, the PI state was
            # ALREADY restored above (`heat_suppressed_this_tick`'s own
            # branch) — this idempotent-heat/idempotent-fan path does
            # nothing further to it, and the cadence timer is deliberately
            # NOT advanced here either (mirrors the pre-existing suppression
            # behaviour exactly: a suppressed tick never counts as a control
            # actuation, so the next tick retries at the same elapsed-time
            # budget).
            if not heat_suppressed_this_tick:
                self._post_fc_last_actuation_monotonic = now
                # D96 slice 2 (#559): this `output` stands (kept, not
                # restored) — stash it for `_build_advisor_context` (told ==
                # enforced).
                self._last_post_fc_output = output
                self._accepted_post_fc_output = output
            # D96 slice 2 (#559), Codex round-1 finding #1 precedent applied
            # here too: on a suppressed tick `_last_post_fc_output` is left
            # exactly as `_force_recovery_exit`/the top-of-tick machinery
            # already set it — never stashed here, since the ACTUATED heat
            # (held) never matches what `output` (the discarded tentative
            # raise) claims.
            return
        # Matrix gate first (SET_HEAT is valid in DEVELOPMENT — no change to
        # COMMAND_PHASE_MATRIX was needed for this slice; see the safety.py
        # row), then the bounds/rate-limit clamp — the identical two-step gate
        # the pre-FC lever and the advisor path both go through.
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.SET_HEAT, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:  # pragma: no cover — DEVELOPMENT
            # is in the SET_HEAT matrix row; unreachable defensive guard (mirrors
            # the identical pre-FC guard above).
            if not heat_suppressed_this_tick:
                self._post_fc_controller.restore_state(pre_compute_state)
            return
        evaluation = self._safety.evaluate_command(
            requested_heat=actuated_heat,
            # #498: the desired fan the advisor's consult already
            # safety-evaluated — re-clamped here into the SAME box this
            # write is evaluated against (told == enforced, #273/#412),
            # never `config.fan_percent` (that pin is retired for the loop's
            # own write; fan is the advisor's lever in loop mode now).
            requested_fan=desired_fan,
            seconds_since_last_command=self._seconds_since_last_command(),
            bounds=box,
        )
        await self._snapshots.persist_evaluation(evaluation)
        executed = await self._execute_targets(evaluation)
        # Key state-advance on ``executed`` — ``_execute_targets``'s own report
        # of whether the write actually reached the roaster — NOT on
        # ``evaluation.verdict`` alone (Codex actuator-failure finding, #405
        # Slice B2 fix round 2). A REJECT (rate-limited) tick is one way
        # ``executed`` comes back False; a TRANSIENT ACTUATOR FAILURE — an
        # ALLOW/CLAMP verdict whose ``set_targets`` call then raises — is the
        # other, and the old verdict-only check could not tell them apart from
        # "the write landed": it would have kept the tentative `compute` state
        # AND advanced the cadence timer on an actuator failure, a phantom
        # advance (the roaster's real heat is unchanged, but the loop's
        # internal state raced ahead as if the command had applied).
        if executed and not heat_suppressed_this_tick:
            # Accepted AND actually executed (and NOT a suppressed-heat/
            # fan-only tick, whose PI state was already restored above and
            # must stay that way — Codex round-2 finding #2): the loop's PI
            # state (already advanced by the `compute` call above) stands,
            # and the cadence timer advances so the NEXT actuation is paced
            # from THIS confirmed instant.
            self._post_fc_last_actuation_monotonic = now
            # D96 slice 2 (#559): this `output` stands (kept, not restored) —
            # stash it for `_build_advisor_context` (told == enforced).
            self._last_post_fc_output = output
            self._accepted_post_fc_output = output
        elif not executed and not heat_suppressed_this_tick:
            # NOT executed — REJECTed (e.g. rate-limited) OR an actuator
            # failure: undo the tentative `compute` step entirely so the
            # integrator/EMA/bias are exactly as they were before this tick
            # ran it (#412 told==enforced extended to a stateful loop — see
            # the method docstring). The cadence timer is NOT advanced either,
            # so the next tick retries at the same elapsed-time budget rather
            # than losing a full cadence interval to a write that never
            # reached the roaster.
            self._post_fc_controller.restore_state(pre_compute_state)
            # D96 slice 2 (#559): `output` was NOT kept — do NOT stash it,
            # mirroring the PI state restore immediately above.

    async def _execute_deterministic_drop(self, reason: DropReason) -> bool:
        """Shared drop-execution sequence for every deterministic (non-advisor,
        non-operator) drop path (D84 Slice C / D88 amendment A1, #405).

        Both :meth:`_maybe_deterministic_drop` (the dev%/temp anchor) and
        :meth:`_maybe_ceiling_guard_drop` (the decoupled ceiling guard) reach
        their eligibility decision independently (different gates, different
        thresholds) but then run the IDENTICAL command sequence: the same
        safety path as every other drop in this codebase —
        ``evaluate_drop_recommendation`` (ALLOWs unconditionally in
        DEVELOPMENT, the only phase either caller invokes this from) then
        ``self._executor.drop_beans()`` in a try/except (``COMMAND_FAILED`` +
        return on a transient actuator failure, mirroring the advisor-drop and
        operator-drop paths exactly), then ``COMMAND_EXECUTED`` and
        ``transition_to(COOLING)``. Factored here so the two callers cannot
        drift apart on this sequence (e.g. one forgetting the safety
        evaluation, or persisting it differently) — only the ELIGIBILITY
        check differs between them, encoded entirely in each caller's own
        conditions before this method is called.

        ``reason`` is carried into the ``COMMAND_EXECUTED``/``COMMAND_FAILED``
        event payload's ``reason`` key (as ``reason.value``) purely for
        observability/trace-reading — it is never compared against in any
        controller or safety code path (D15: no bare string comparison in core
        logic), only ever constructed from and read back as the typed
        :class:`~roastpilot_agent.models.DropReason` value.

        Args:
            reason: Which caller is asking this drop to fire.

        Returns:
            ``True`` if the drop was recommended, executed, and the phase
            transitioned to COOLING; ``False`` if the safety evaluation
            rejected it (unreachable today — see the inline comment) or the
            executor raised (a transient actuator failure, already reported
            via ``COMMAND_FAILED``).
        """
        drop = self._safety.evaluate_drop_recommendation(phase=self._phase)
        await self._snapshots.persist_evaluation(drop)
        # evaluate_drop_recommendation ALLOWs unconditionally in DEVELOPMENT (the
        # sole phase either caller actuates in) — the REJECT/false branch here is
        # unreachable today. Kept (not collapsed) as the safety boundary if a
        # future phase becomes reachable by either caller; the un-taken branch is
        # pragma'd, not the guard logic (mirrors the identical advisor-drop
        # pattern in ``_run_advisory``).
        if drop.verdict is not SafetyVerdict.ALLOW:  # pragma: no cover — see above
            return False
        try:
            applied = await self._executor.drop_beans()
        except Exception:
            self._events.emit(
                RoastEventKind.COMMAND_FAILED,
                {"command": "drop_beans", "source": "policy", "reason": reason.value},
            )
            # D96 slice 1.5 (#561): a failed drop while recovery authority is
            # elevated must not leave the roaster sitting at raised heat —
            # force it back to the D96/D88 base through the safety path.
            # `self_healing=True`: this path (shared by the ceiling-guard and
            # dev%/temp-anchor callers) fires on EVERY DEVELOPMENT tick with
            # no cadence gate, so a failed corrective write is retried again
            # next tick regardless (Codex round-2 finding #1's distinction).
            await self._clamp_heat_after_failed_drop(self_healing=True)
            return False
        self._adopt_applied_state(applied)
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED,
            {"command": "drop_beans", "source": "policy", "reason": reason.value},
        )
        self.transition_to(RoastPhase.COOLING)
        return True

    async def _clamp_heat_after_failed_drop(self, *, self_healing: bool) -> None:
        """D96 slice 1.5 (#561): fail-safe-DOWN heat clamp on a FAILED drop
        while D96 recovery authority is elevated.

        PR #560's own raise-suppression (the skip inside
        :meth:`_apply_deterministic_post_fc_levers`) stops a NEW raise from
        reaching the roaster on a drop-eligible tick, and round 4 additionally
        let a hold-or-lower write pass through so a failed drop under GLIDING
        still descends on its own. The residual #561 identified: the SINGLE
        tick where recovery CONFIRMS entry and a drop is independently
        eligible the same tick still lands a genuine raise (the entry write
        happens before either drop path runs — see ``tick()``'s order) — if
        the drop that same tick then FAILS, the roast is left in DEVELOPMENT
        holding that raised value with no further loop tick guaranteed to pull
        it back down promptly (the loop's own cadence and deadband could
        leave it elevated for several more seconds while every subsequent
        tick keeps trying, and failing, the same drop).

        This method is the ACTIVE companion to that passive glide: called from
        every drop-failure branch — :meth:`_execute_deterministic_drop`
        (shared by the ceiling-guard and dev%/temp-anchor paths), the
        advisor drop path in :meth:`_run_advisory`, and (Codex round-1
        finding #2) the OPERATOR's own drop in :meth:`operator_drop_beans`,
        scoped there to the non-``FAULTED`` (``will_transition``) case only
        — it forces heat back to the D96/D88
        base — :attr:`~roastpilot_agent.post_fc_control.PostFcControllerState.
        heat_engage_percent`, the never-add-heat-beyond-entry anchor D96's
        recovery ceiling itself is built from — THROUGH THE SAME SAFETY PATH
        as every other roaster write (``evaluate_command_phase`` then
        ``evaluate_command`` with a single-source box, mirroring
        :meth:`_apply_deterministic_post_fc_levers`'s own write exactly).
        Never a raw/bypassing write. The ONE deliberate deviation:
        ``evaluate_command`` is called with ``seconds_since_last_command=
        None`` — exempting only the ordinary command RATE LIMIT (never the
        bounds/CLAMP check or the phase-matrix gate) — because the scenario
        this method exists for is EXACTLY a same-tick collision: the raise
        needing undoing is very often the immediately-preceding accepted
        command this same tick (the loop's own write, a moment before the
        drop attempt that then failed), so honouring the ordinary rate limit
        here would silently swallow the corrective write on precisely the
        tick it matters most. Safe because this write can only ever move
        heat DOWN to a value the roast already legitimately held (see the
        idempotence check below) — see the inline comment at the
        ``evaluate_command`` call for the full reasoning.

        **``self_healing`` (Codex round-2 finding #1 — the failed-corrective-
        write asymmetry):** the two DETERMINISTIC drop paths
        (:meth:`_execute_deterministic_drop`) fire on EVERY DEVELOPMENT tick
        with no cadence gate as long as their eligibility condition holds —
        if this clamp's own corrective write ALSO fails, the very next tick
        re-attempts both the drop AND the clamp, so an unrecovered elevated
        state is bounded to (at most) one tick before another attempt. The
        advisor path is cadence-gated (:class:`AdvisoryCallPolicy` — could be
        many seconds before the next consult) and the operator path is
        one-shot (never automatically retried at all): on THOSE two paths, a
        failed corrective write can leave the roast holding recovery-raised
        heat with no further correction attempt coming for an unbounded
        stretch, and the latch that is supposed to prevent a re-raise
        (:attr:`_post_fc_raise_suppressed_after_clamp`) never arms either,
        since arming happens only inside :meth:`_force_recovery_exit`, which
        (correctly, per the #412 discipline) only runs on a write that
        actually landed. ``self_healing=False`` (the advisor and operator
        callers) arms the latch regardless of whether the write below
        succeeds — reproduced directly before this fix: a hand-driven
        failed-corrective-write tick on the advisor path left heat at its
        raised value with the latch unarmed, and a run of sub-cadence ticks
        (no re-consult) never corrected it. ``self_healing=True`` (the
        deterministic callers) is UNCHANGED — they keep the pre-existing
        behaviour (arm only on a landed write) because they do not need the
        independent latch: the next tick's own retry already re-attempts
        the clamp, and arming early there would gain nothing while adding a
        second code path to reason about for no benefit.

        **Scope (elevated-authority ticks only):** a no-op unless
        ``self._last_post_fc_output.heat_authority_state is not HOLDING`` —
        flag-off engagements and HOLDING (plain-D88) engagements never even
        reach this check with a non-``None``, non-``HOLDING`` output, so their
        behaviour is byte-for-byte unchanged (the #560 discipline). This also
        means the method is naturally inert when
        ``post_first_crack_control.enabled`` is ``False`` or the loop never
        engaged this DEVELOPMENT dwell: :attr:`_last_post_fc_output` stays
        ``None`` on both paths (cleared at disengage, never set while the
        loop's own top-of-function gate no-ops it).

        **Fail-safe-DOWN, never up:** the write only ever LOWERS heat. When
        ``self._current_heat`` is already at or below the base
        (``self._current_heat <= base``), there is nothing to clamp down to —
        skip entirely (idempotence: a repeated failed-drop tick with heat
        already at base issues no redundant write).

        **Fan is untouched:** this is a heat-only fail-safe (D96's own scope);
        the write holds fan at ``self._current_fan`` exactly, never reasoning
        about the advisor's fan lever at all.

        **Recovery re-arm (design decision, tick-table-justified):** on a
        successful clamp write this method ALSO forces the recovery state
        machine to fully EXIT — ``recovery_active=False``,
        ``recovery_ticks_since_exit=None``, both confirm-tick counters zeroed
        — via :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.
        restore_state` with a synthesized state that carries the CURRENT
        (post-clamp) integrator/EMA/taper fields forward unchanged and only
        overwrites the four recovery-specific fields. Justification from the
        tick tables: leaving the counters as "confirmed active/gliding" after
        forcibly overriding the ceiling's own output would let the very next
        tick's :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.
        compute` call reason from a ceiling that no longer matches what the
        roaster actually holds (``effective_ceiling`` stays at the recovery
        value while ``_current_heat`` was just forced to the D88 base beneath
        it) — a one-tick state/reality gap the #412 told==enforced rule
        exists to prevent everywhere else in this codebase. Resetting clears
        that gap and requires a FRESH ``recovery_confirm_ticks`` run before
        authority elevates again, which is also the correct bounded-re-arm
        behaviour for repeated failures: if ``drop_beans()`` keeps failing
        every tick, each clamp re-confirms heat at the base and re-arms
        cleanly rather than compounding a raise/clamp thrash — the roast
        continues (the drop keeps being retried by the caller's own no-cadence
        eligibility check), heat descends and STAYS at base across repeated
        failures, and recovery can only re-engage after a genuine fresh
        RoR-shortfall confirmation, never as an artifact of the failed drop
        itself.

        **Invariants held (unchanged by this method):** every write passes
        the identical safety path as every other roaster write; the 196 °C
        bitter ceiling / emergency-drop bound / e-stop live in
        ``SafetyPolicy``'s temperature rules and are untouched here; emergency
        stop remains reachable from every phase; a restart never auto-resumes
        into this path (never reachable from ``operator_recovery_required``).
        Reachable from ``ROASTING_PRE_FIRST_CRACK`` too, via the operator's
        own drop (Codex round-1 finding #2, the pre-FC early-abort case) —
        harmlessly inert there: ``_last_post_fc_output`` is ``None`` in every
        pre-FC phase (the loop only ever engages via the true FC edge, and
        ``transition_to`` clears it on every other transition), so the
        top-of-function ``output is None`` guard makes this a genuine no-op
        outside DEVELOPMENT.
        """
        output = self._last_post_fc_output
        if output is None or output.heat_authority_state is PostFcHeatAuthorityState.HOLDING:
            return
        state = self._post_fc_controller.snapshot_state()
        base = state.heat_engage_percent
        if self._current_heat <= base:
            # Fail-safe-DOWN idempotence: already at or below the base — no
            # write, but the recovery state machine is still forced to exit
            # below (a failed drop with heat already settled at base is still
            # a "stop trying to elevate" signal for the next tick).
            self._force_recovery_exit(state)
            return
        # safety-561 (Opus, Low-1): the base box's `heat_ceiling_percent` is
        # NOT independently pinned here — DEVELOPMENT resolves it to 100
        # today, so `base <= ceiling` always holds, but a future config that
        # narrowed DEVELOPMENT's heat_ceiling below `base` would raise
        # `PhaseControlLimits`'s floor > ceiling validator UNCAUGHT inside
        # `tick()`. This clamp only ever LOWERS heat to `base` (never raises
        # it — the idempotence check above already ensures `base <
        # self._current_heat`), so the box's ceiling must never be allowed to
        # constrain that target: widen it defensively to
        # `max(base, resolved_ceiling)` so the floor/ceiling relationship the
        # write depends on can never invert, regardless of any future
        # DEVELOPMENT config. D157 deliberately leaves the fan side unnarrowed
        # too: this corrective passes requested_fan=self._current_fan and is a
        # heat-down-only write. Narrowing fan here could silently cut an already-
        # actuated fan above the consult ceiling, a lever move decided by no actor.
        resolved_box = self._control_limits()
        box = resolved_box.model_copy(
            update={
                "heat_floor_percent": base,
                "heat_ceiling_percent": max(base, resolved_box.heat_ceiling_percent),
                "heat_target_percent": base,
            }
        )
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.SET_HEAT, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:  # pragma: no cover — DEVELOPMENT
            # is in the SET_HEAT matrix row; unreachable defensive guard (mirrors
            # the identical pre-FC/post-FC-loop guards above).
            return
        evaluation = self._safety.evaluate_command(
            requested_heat=base,
            requested_fan=self._current_fan,
            # `seconds_since_last_command=None` DELIBERATELY exempts only the
            # RATE LIMIT (never the bounds/CLAMP check, never the phase-matrix
            # gate above) — the exact scenario this clamp exists for is a
            # SAME-TICK collision: the raise that needs undoing was itself
            # the previous accepted command this tick (`_last_command_
            # monotonic` was just set by the loop's own write, so `
            # _seconds_since_last_command()` would read ~0 and the ordinary
            # `command_rate_limited` REJECT would silently swallow this write
            # every time it matters most). Safe to exempt because this write
            # can ONLY ever move heat DOWN (the idempotence check above
            # already ensures `base < self._current_heat`, and `base` is
            # itself a value the roast already held at some prior instant,
            # never a fabricated one) — the rate limit exists to protect the
            # ~1 Hz Hottop serial loop from a THRASHING thrash of writes, not
            # from a single corrective descent immediately following the
            # write it corrects. This mirrors `_apply_fail_safe`'s own
            # bypass of ordinary write gating for a fail-closed heat-down
            # action, applied here to the ONE additional gate
            # (`evaluate_command`) `_apply_fail_safe` does not use at all.
            seconds_since_last_command=None,
            bounds=box,
        )
        await self._snapshots.persist_evaluation(evaluation)
        executed = await self._execute_targets(evaluation)
        if executed:
            self._force_recovery_exit(state)
        elif not self_healing:
            # Codex round-2 finding #1: the write FAILED (the roaster's real
            # heat is unchanged — mirrors every other actuator-failure path
            # in this module, and the recovery COUNTERS/PI state are left
            # untouched here exactly as they would be on the self-healing
            # paths, per the #412 discipline: state changes only on a write
            # that actually landed) — but on a non-self-healing caller
            # (advisor/operator) there is no guaranteed next-tick retry to
            # eventually re-arm the suppression, so arm it HERE,
            # independently of whether the counters themselves got reset.
            # This is the ONE deliberate asymmetry `self_healing` encodes:
            # the deterministic callers skip this branch entirely (they
            # retry every tick regardless, and arming early there would
            # gain nothing).
            self._post_fc_raise_suppressed_after_clamp = True
        # A failed clamp write on a SELF-HEALING path leaves the recovery
        # state machine exactly as it was (mirrors every other actuator-
        # failure path in this module): the roaster's real heat is
        # unchanged, so there is no state/reality gap to close yet — the
        # next tick's own drop-eligibility retry, and this same clamp, will
        # be attempted again.

    def _force_recovery_exit(self, state: PostFcControllerState) -> None:
        """Force the D96 recovery state machine to HOLDING (#561).

        Carries every non-recovery field of ``state`` forward unchanged
        (integrator, bias, EMA, taper clock/r0, ``heat_engage_percent``) and
        overwrites only the four recovery-specific fields to their fully-
        exited values, via
        :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.
        restore_state`. Called only after a successful (or already-idempotent)
        post-failure heat clamp — see :meth:`_clamp_heat_after_failed_drop`
        for the tick-table justification of forcing a full re-arm rather than
        leaving the confirmed active/gliding counters in place.

        **Also arms the persistent raise-suppression latch** (Codex round-1
        finding #3): resetting the recovery counters here closes the
        ceiling/reality gap (#412) but ALSO clears
        ``heat_authority_state``'s own "elevated" signal — which a
        persisting RoR shortfall can re-confirm as soon as the very next
        tick. Setting ``self._post_fc_raise_suppressed_after_clamp`` here
        means :meth:`_apply_deterministic_post_fc_levers` keeps suppressing
        every raise regardless of that re-confirmation (and regardless of
        this tick's own drop-eligibility, unlike the pre-existing same-tick
        mirrors) until a drop finally succeeds or the dwell ends — every
        call site of this method represents a drop that just failed while
        heat needed to be at or below the base, which is exactly the
        condition the latch exists to remember.

        **Also clears the stashed ``_last_post_fc_output`` and the tick-scoped
        ``_accepted_post_fc_output`` witness** (Codex round-1 finding #1 / PR
        #700 safety review): the former is copied VERBATIM into
        :meth:`_build_advisor_context` (told == enforced, D96 slice 2) —
        without this, a tick where a drop fails AFTER the loop already
        stashed a RECOVERING/GLIDING output earlier the SAME tick would let
        the advisor (and the decision trace) see stale elevated-authority
        diagnostics for a state this method just reset to HOLDING beneath
        it. ``None`` is exactly the correct value here: this method runs
        AFTER the loop's own step for the tick, so there is no fresh,
        still-valid ``PostFcControlOutput`` to stash instead — every reader
        of this field (``_build_advisor_context``'s two ``None``-mapped
        fields) already treats ``None`` as "the loop's state this tick is
        not meaningfully known", the correct posture for a forced reset.
        The accepted witness is likewise no longer the tick's final
        authoritative D96 state once the corrective clamp has landed and the
        controller has forced authority back to HOLDING; retaining it would
        make persistence report an elevated state the clamp superseded. A
        failed corrective write never calls this method, so its still-real
        elevated witness remains intact.

        Args:
            state: A snapshot taken in the same clamp call this re-arm
                belongs to (``PostFcRorController.snapshot_state()``), so the
                non-recovery fields it carries are this tick's own — never a
                stale snapshot from an earlier tick.
        """
        self._post_fc_controller.restore_state(
            state.__class__(
                integrator=state.integrator,
                bias_percent=state.bias_percent,
                ema=state.ema,
                taper_elapsed_seconds=state.taper_elapsed_seconds,
                taper_r0_c_per_min=state.taper_r0_c_per_min,
                heat_engage_percent=state.heat_engage_percent,
                recovery_ticks_above_trigger=0,
                recovery_ticks_within_exit=0,
                recovery_active=False,
                recovery_ticks_since_exit=None,
            )
        )
        self._post_fc_raise_suppressed_after_clamp = True
        self._last_post_fc_output = None
        self._accepted_post_fc_output = None

    async def _maybe_ceiling_guard_drop(self, telemetry: RoastTelemetry | None) -> None:
        """Decoupled ceiling-guard drop: a bitter-line safety anchor, not a
        taper feature (D88 amendment A1, #405).

        Gated end-to-end ONLY on ``post_first_crack_control.ceiling_guard_drop_enabled``
        (its OWN flag, D88 amendment A2) AND phase DEVELOPMENT — deliberately
        NOT on the RoR-taper loop's ``enabled`` flag or ``self._post_fc_engaged``.
        This is the point of the decoupling: a taper-gated guard would leave
        every taper-flag-OFF roast (today's default, and likely for some time
        after the taper itself is validated) and every post-recovery resume
        (where ``_post_fc_engaged`` is False even with the taper flag on) with
        NO deterministic bitter-line protection at all — the 196 °C boundary
        would still be owned solely by the advisor's own judgment, exactly as
        it is today. Default ``False`` — a byte-for-byte no-op on every path
        until an operator consciously flips it (a separately reviewed
        incumbent-behaviour change; that flip is not part of this change).

        **Eligibility:** fires the instant ``telemetry.bean_temp_c >=
        ceiling_guard_temp_c`` while in DEVELOPMENT — no development-percent
        condition, no engagement condition, no cadence gate (mirrors
        :meth:`_maybe_deterministic_drop`'s one-shot-per-tick reasoning: a drop
        is irreversible, so it must fire the instant it is eligible).

        **Ordering (called BEFORE the dev%/temp anchor):** this is the harder
        of the two safety bounds the D88 row identifies (the ratifier's
        rationale: a run where the loop keeps chasing heat even harder could
        cross the bean-temp leg alone while dev% still lags — the ceiling must
        be able to force the drop independently). Running it first means a
        tick where BOTH this guard and the dev%/temp anchor would be eligible
        still drops via the guard's own reason; the anchor's own phase guard
        (``self._phase is not RoastPhase.DEVELOPMENT``) then makes
        :meth:`_maybe_deterministic_drop` a clean no-op the same tick, since
        this method already transitioned the phase to COOLING — the two
        deterministic drop paths can never both fire in one tick, the same
        guarantee that already holds between the anchor and the advisor's own
        drop path.

        **Invariants held (unchanged by this method):** the operator's manual
        DROP BEANS stays UN-gated; the 196 °C bitter ceiling / 198 °C
        emergency-drop bound / e-stop live in ``SafetyPolicy``'s temperature
        rules (``_evaluate_safety``), which run earlier in ``tick()`` and are
        untouched here — this method can only ever fire a drop EARLIER than
        those hard bounds would otherwise force it, never loosen them; the
        drop routes through the SAME ``evaluate_drop_recommendation`` +
        executor path as every other drop
        (:meth:`_execute_deterministic_drop`); emergency stop remains
        reachable from every phase; a restart never auto-resumes into this
        path (``recover_from_restart`` always lands in
        ``operator_recovery_required``, and this method only ever actuates
        from DEVELOPMENT, which a restart never re-enters directly).

        Args:
            telemetry: The reading this tick consumed, or ``None`` on a
                failed/sessionless read (the guard does not fire either way —
                bean temperature is unknown).
        """
        config = self._config.post_first_crack_control
        if (
            not config.ceiling_guard_drop_enabled
            or self._phase is not RoastPhase.DEVELOPMENT
            or telemetry is None
        ):
            return
        if telemetry.bean_temp_c < config.ceiling_guard_temp_c:
            return
        await self._execute_deterministic_drop(DropReason.CEILING_GUARD)

    async def _maybe_deterministic_drop(self, telemetry: RoastTelemetry | None) -> None:
        """Deterministic drop anchor: guarantees the drop lands at target (D84, #405 Slice C).

        Gated end-to-end on the SAME bundle as the post-FC RoR loop
        (``post_first_crack_control.enabled`` AND ``self._post_fc_engaged`` AND
        phase DEVELOPMENT) — when the flag is off, or DEVELOPMENT was reached via
        an operator resume rather than the true FC edge, this method is a pure
        no-op on every path, so flag-off behaviour is byte-for-byte identical to
        today's fully advisor-driven drop.

        **Precedence (D84):** a profile's explicit ``target_drop_temp_c`` /
        ``target_development_percent`` are AUTHORITATIVE for the drop.
        ``roast_style`` (Slice A) does NOT drive the drop at runtime — those two
        fields are required on every profile, so this method reads them
        directly and never consults ``roast_style``.

        **Eligibility (fires the instant BOTH hold):**

            ``telemetry.bean_temp_c >= profile.target_drop_temp_c``
            AND
            ``system_dev_percent >= profile.target_development_percent``

        where ``system_dev_percent`` is :meth:`_development_percent` — the
        charge/FC-referenced SYSTEM value the #313 coherence guard uses — NEVER
        the advisor's claimed development percent (the first supervised roast
        showed that number can be fabricated). ``None`` (development not yet
        computable) fails closed to no-op, never to a drop.

        **LLM-earlier-only:** this method does not change or remove the
        existing advisor ``should_drop`` path in :meth:`_run_advisory` (the
        #313-coherence-gated earlier window, ``dev% >= target -
        drop_dev_margin_percent``). Together the drop fires at
        ``min(advisor-earlier-within-margin, this-anchor)`` — the advisor can
        only pull the drop earlier, never delay it past target.

        **One-shot event, not rate-limited:** unlike the pre-FC/post-FC levers
        (idempotent per-tick writes gated on a control cadence), a drop is a
        single irreversible action, so this runs on EVERY DEVELOPMENT tick with
        no cadence gate — the drop must fire the instant it is eligible, not
        wait for the next 5 s control tick.

        **Same command path as every other drop:** delegates the actual
        evaluate/execute/emit/transition sequence to
        :meth:`_execute_deterministic_drop` (shared with
        :meth:`_maybe_ceiling_guard_drop`, D88 amendment A1) with
        ``DropReason.DEVELOPMENT_TARGET`` — ``evaluate_drop_recommendation``
        (ALLOWs unconditionally in DEVELOPMENT) then
        ``self._executor.drop_beans()`` in a try/except (``COMMAND_FAILED`` +
        return on a transient actuator failure, mirroring the advisor-drop and
        operator-drop paths exactly), then ``COMMAND_EXECUTED`` and
        ``transition_to(COOLING)``. The evaluation is persisted like every
        other drop path (#167 trace parity).

        **Invariants held (unchanged by this method):** the operator's manual
        DROP BEANS stays UN-gated (the backstop, ``operator_drop_beans``,
        unaffected); the 196 °C bitter ceiling / emergency-drop bound / e-stop
        live in ``SafetyPolicy``'s temperature rules (``_evaluate_safety``),
        which run earlier in ``tick()`` and are untouched here; the drop routes
        through the existing ``evaluate_drop_recommendation`` + executor path,
        not a new one; emergency stop remains reachable from every phase (this
        method issues ``drop_beans`` only, never touches e-stop); a restart
        never auto-resumes into this path (``recover_from_restart`` always
        lands in ``operator_recovery_required``, and an operator resume into
        DEVELOPMENT leaves ``_post_fc_engaged`` False, so this method stays
        inert there — the advisor-only drop path is the resume fallback,
        exactly as it is for the RoR loop). The decoupled ceiling guard
        (:meth:`_maybe_ceiling_guard_drop`) is a SEPARATE, unconditional safety
        anchor that does NOT share this method's gating — see its own
        docstring.

        Args:
            telemetry: The reading this tick consumed, or ``None`` on a
                failed/sessionless read (the anchor does not fire either way —
                bean temperature is unknown).
        """
        config = self._config.post_first_crack_control
        if (
            not config.enabled
            or not self._post_fc_engaged
            or self._phase is not RoastPhase.DEVELOPMENT
            or self._profile is None
            or telemetry is None
        ):
            return
        system_dev_percent = self._development_percent()
        if system_dev_percent is None:
            # Fail safe: never drop on unknown development. Mirrors the
            # coherence guard's fail-OPEN-for-the-advisor / fail-CLOSED-for-
            # this-anchor asymmetry — the advisor path fails open (lets the
            # model's drop through) when development is unknown because the
            # safety box still owns the phase boundary there, but THIS anchor
            # only ever produces a drop, never blocks one, so failing closed
            # (no-op) is the safe direction.
            return
        if (
            telemetry.bean_temp_c < self._profile.target_drop_temp_c
            or system_dev_percent < self._profile.target_development_percent
        ):
            return
        await self._execute_deterministic_drop(DropReason.DEVELOPMENT_TARGET)

    async def _apply_phase_rules(self, telemetry: RoastTelemetry | None) -> None:
        """MCP-driven phase rules: preheating (E4-S3) and roasting (E4-S4).

        Preheating: add-beans guidance is emitted exactly once when the bean
        temperature enters the profile's charge guidance band — guidance, not
        a blocking operator-required state. (#211: the env clause is dropped;
        on an empty drum the env probe leads the bean probe, which mistimed
        the cue — it now tracks the bean probe the operator watches and that
        auto-T0 uses.) The T0 debounce
        counts consecutive ticks of MCP-reported T0 and resets on absence
        *or* on a read-fault tick (plan §2: flapping originates from read
        faults — MCP latches detection internally); the transition commits
        only after ``t0_debounce_ticks`` consecutive confirmations.

        Roasting: first crack is a latched MCP event (no debounce). Both
        transitions stamp their true detection source (MCP) through
        evaluate_event_source (E3-S5/D16 — the controller relays, it never
        re-stamps).
        """
        if self._phase is RoastPhase.PREHEATING:
            if telemetry is None:
                self._reset_t0_debounce()  # a failed/absent read breaks the window
                return
            self._maybe_emit_charge_guidance(telemetry)
            if telemetry.t0_detected:
                if self._t0_streak == 0:
                    # Origin the charge clock on the FIRST detect tick (#174), not
                    # the later debounced transition.
                    self._t0_first_detect_monotonic = self._clock()
                self._t0_streak += 1
                # Latch the MCP turning-point backdate the first tick it appears
                # (#174/#337): the ``beans_added`` event can race in a tick after
                # ``t0_status`` flips, so reading it only at the transition missed it.
                if telemetry.t0_backdate_seconds is not None:
                    self._pending_t0_backdate = telemetry.t0_backdate_seconds
            else:
                self._reset_t0_debounce()
            if self._t0_streak >= self._config.t0_debounce_ticks:
                source = self._safety.evaluate_event_source(
                    transition="t0", source=RoastEventSource.MCP
                )
                await self._snapshots.persist_evaluation(source)
                if source.verdict is not SafetyVerdict.ALLOW:
                    return
                self._t0_confirmed = True
                # Cause before effect: consumers see T0_DETECTED, then the
                # PHASE_CHANGED it explains (review note, E4-S3 PR).
                self._events.emit(
                    RoastEventKind.T0_DETECTED,
                    {"debounce_ticks": self._t0_streak, "bean_temp_c": telemetry.bean_temp_c},
                )
                self._t0_streak = 0  # unambiguous post-confirmation state
                self.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
                # Stamp the charge clock at the debounced T0 transition: the
                # advisor's ``seconds_since_charge`` context and the
                # charge-referenced DTR clock (#219) read from this instant. The
                # post-charge settle window (#209) is retired under D35 (#222) —
                # the advisor is no longer consulted pre-FC, so there is no
                # automatic post-charge consult left to suppress.
                #
                # #174/#337: origin the charge clock on the FIRST detect tick of
                # the debounce streak (un-debounced), backdated to the MCP TURNING
                # POINT (the local-max bean temp before the decline) — NOT this
                # debounced transition tick. Two corrections compose: (1) the
                # agent's own ``t0_debounce_ticks`` debounce, by anchoring to the
                # latched first detect; (2) the MCP confirmation lag, by subtracting
                # the latched turning-point delta (confirmed − turning point). A
                # manual mark / pre-0.1.7 payload carries no delta ⇒ no backdate,
                # but the first-detect anchor still removes the debounce. Shifts
                # dev%/DTR/curve x-origin earlier (the lag corrected at T0): one
                # roast stamped T0 ~10 s late at bean 150 °C vs the 179 °C peak.
                self._charge_monotonic = self._charge_origin_monotonic(
                    self._t0_first_detect_monotonic, self._pending_t0_backdate
                )
                self._t0_first_detect_monotonic = None
                self._pending_t0_backdate = None
            return
        if self._phase is RoastPhase.ROASTING_PRE_FIRST_CRACK and telemetry is not None:
            # Drying-end signal (#351) BEFORE the FC check: a pre-FC observability
            # landmark, so it can only fire while still pre-FC — the FC transition
            # below moves the phase out of pre-FC and ends the window.
            self._maybe_emit_drying_end(telemetry)
            if telemetry.first_crack_detected:
                source = self._safety.evaluate_event_source(
                    transition="first_crack", source=RoastEventSource.MCP
                )
                await self._snapshots.persist_evaluation(source)
                if source.verdict is not SafetyVerdict.ALLOW:
                    return
                self._events.emit(
                    RoastEventKind.FIRST_CRACK,
                    {"source": RoastEventSource.MCP.value, "bean_temp_c": telemetry.bean_temp_c},
                )
                # #337: stage the MCP-reported FC backdating delta so the
                # development-clock stamp in ``transition_to`` origins on the crack
                # ONSET, not this receive-tick. Staged because ``transition_to`` is
                # generic (no telemetry in scope); it is consumed-and-cleared at the
                # stamp. Only this MCP-detection FC path stages a delta — the
                # operator override (``operator_mark_first_crack``) leaves it None
                # ⇒ stamp at receive-tick.
                self._pending_first_crack_backdate = telemetry.first_crack_backdate_seconds
                self.transition_to(RoastPhase.DEVELOPMENT)

    def _maybe_emit_charge_guidance(self, telemetry: RoastTelemetry) -> None:
        """Emit the one-shot add-beans cue when the BEAN probe enters the band.

        The trigger keys on ``bean_temp_c`` only (#211). On an empty preheating
        drum the environment probe leads the bean probe, so an env-or-bean
        trigger fired the cue early — while the bean probe the operator watches
        ("when we hit 170") was still below the band. Keying on the bean probe
        aligns the cue with both the reading the operator acts on and the signal
        auto-T0 uses. The emitted payload still carries ``env_temp_c`` (and the
        band bounds) for the decision trace; only the trigger field changed.

        Args:
            telemetry: The latest validated reading for this tick.
        """
        if self._guidance_emitted or self._profile is None:
            return
        low = self._profile.charge_guidance_min_c
        high = self._profile.charge_guidance_max_c
        if low <= telemetry.bean_temp_c <= high:
            self._guidance_emitted = True
            self._events.emit(
                RoastEventKind.CHARGE_GUIDANCE,
                {
                    "bean_temp_c": telemetry.bean_temp_c,
                    "env_temp_c": telemetry.env_temp_c,
                    "guidance_min_c": low,
                    "guidance_max_c": high,
                },
            )

    def _maybe_emit_drying_end(self, telemetry: RoastTelemetry) -> None:
        """Emit the one-shot pre-FC drying-end signal on the first threshold cross.

        Fires once, the tick the bean probe first reaches
        :attr:`ControllerConfig.drying_end_bean_temp_c` (default 150 °C, the
        .alog-validated drying→browning landmark — see the config docstring). A
        one-way latch (``_drying_end_emitted``) so it never re-fires within a run,
        and it is gated behind the TURNING POINT having already been recorded:
        post-charge the bean crashes well below 150 and climbs back through it, so
        requiring the curve to have bottomed out first makes the cross noise-robust
        — a single jittery sample during the crash (or a hot charge reading) cannot
        fire it, only the genuine rising cross of a bean that has turned. The caller
        only invokes this pre-FC, so the window closes at first crack.

        Emitted as a :class:`RoastEventKind.DRYING_END` event → the SSE stream (the
        live chart marker) and the persisted timeline (detail page). Observability
        ONLY: it is NOT recorded as a :class:`RoastMilestone`, so it never enters
        :attr:`AdvisorContext.roast_milestones` — the advisor and every safety/
        control path are untouched by it (lead constraint, #351). Temperatures
        Celsius.

        Args:
            telemetry: The latest validated pre-FC reading for this tick.
        """
        if self._drying_end_emitted:
            return
        # Noise-robust gate: only after the bean has turned (post-charge minimum
        # passed). Without it a transient high sample during the post-charge crash
        # could trip the threshold spuriously.
        if not self._history.has_milestone(RoastMilestoneKind.TURNING_POINT):
            return
        if telemetry.bean_temp_c >= self._config.drying_end_bean_temp_c:
            self._drying_end_emitted = True
            self._events.emit(
                RoastEventKind.DRYING_END,
                {
                    "bean_temp_c": telemetry.bean_temp_c,
                    "threshold_c": self._config.drying_end_bean_temp_c,
                },
            )

    async def _read_telemetry(self) -> tuple[RoastTelemetry | None, bool]:
        """Read MCP state; a raised read is counted, a clean None is not
        (no session is a validity concern, not an MCP-health one)."""
        try:
            telemetry = await self._reader.read_telemetry()
        except Exception:
            self._consecutive_read_failures += 1
            return None, True
        self._consecutive_read_failures = 0
        return telemetry, False

    def _evaluate_safety(
        self, telemetry: RoastTelemetry | None, *, read_failed: bool
    ) -> SafetyEvaluation:
        """Safety order: MCP read health, telemetry validity, temperatures.

        A tolerated read failure short-circuits the tick's remaining
        telemetry rules (orchestration plan: skip the tick and continue) —
        a known-failed read must not double-report as missing telemetry.
        """
        if read_failed:
            return self._safety.evaluate_mcp_failure(
                operation="read",
                consecutive_failures=self._consecutive_read_failures,
            )
        validity = self._safety.evaluate_telemetry_validity(
            phase=self._phase,
            telemetry_age_seconds=None if telemetry is None else telemetry.age_seconds,
            max_stale_seconds=self._config.max_stale_telemetry_seconds,
        )
        if validity.verdict is not SafetyVerdict.ALLOW or telemetry is None:
            return validity
        return self._safety.evaluate_telemetry(
            phase=self._phase,
            bean_temp_c=telemetry.bean_temp_c,
            env_temp_c=telemetry.env_temp_c,
            # The real debounced confirmation (E4-S3) — replaces the E4-S2
            # phase-identity proxy per the safety-reviewer carry-forward.
            t0_confirmed=self._t0_confirmed,
        )

    async def _act_on_safety(self, evaluation: SafetyEvaluation) -> bool:
        """Map a telemetry-stage verdict to fail-closed action. Returns True
        when the tick must stop (no advisory, no further commands).

        Hardware-off guarantees on faulted/recovery entry are hardened in
        E4-S4; this stage owns the transition + e-stop execution.
        """
        verdict = evaluation.verdict
        if verdict is SafetyVerdict.ALLOW:
            return False
        if verdict is SafetyVerdict.EMERGENCY_STOP:
            try:
                applied = await self._executor.emergency_stop(reason=evaluation.reason)
            except Exception:
                # A raising e-stop must not crash the tick loop or leave
                # the phase pre-fault: fault anyway and surface the failed
                # command. Timeout-bounding the call itself is E5's
                # criterion; retry/fallback hardening is E4-S4's.
                self._events.emit(
                    RoastEventKind.COMMAND_FAILED,
                    {"command": "emergency_stop", "reason": evaluation.reason},
                )
                # The e-stop did not confirm; latch a heat-off retry so the
                # terminal-phase tick keeps driving toward safety rather than
                # leaving the roaster potentially hot (#206 / PR review blocker).
                self._pending_fail_safe = self._heat_off_evaluation(
                    source_rule="emergency_stop_retry"
                )
            else:
                # Confirmed on hardware (#507): adopt the applied heat/fan so the
                # mirrors — and everything downstream reading them — reflect the
                # e-stop immediately, the same tick, instead of the last pre-stop
                # set_targets values.
                self._adopt_applied_state(applied)
            # Emit ONCE, only on entry into FAULTED — never while already
            # latched there (the tick loop short-circuits before reaching here
            # in a terminal phase, but the guard keeps the emit transition-bound
            # regardless of caller; #206 infinite-loop fix).
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
                self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            self._latched_verdict = SafetyVerdict.EMERGENCY_STOP
            return True
        if verdict is SafetyVerdict.FAULT:
            await self._apply_fail_safe(evaluation)
            # Emit ONCE, only on entry into FAULTED (see EMERGENCY_STOP above).
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
                self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            self._latched_verdict = SafetyVerdict.FAULT
            return True
        if verdict is SafetyVerdict.RECOVERY:
            await self._apply_fail_safe(evaluation)
            # Emit ONCE, only on entry into OPERATOR_RECOVERY_REQUIRED.
            if self._phase is not RoastPhase.OPERATOR_RECOVERY_REQUIRED:
                self.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
                self._events.emit(
                    RoastEventKind.RECOVERY_REQUIRED, evaluation.model_dump(mode="json")
                )
            self._latched_verdict = SafetyVerdict.RECOVERY
            return True
        # CLAMP/REJECT never arise from telemetry-stage rules.
        return False

    async def _apply_fail_safe(self, evaluation: SafetyEvaluation) -> None:
        """Hardware-off on faulted/recovery entry (E3-S2/E4 carry-forward).

        Applies the evaluation's adjusted values (heat 0 %, safe fan)
        before the transition commits. The safety evaluation itself is the
        authority here — this deliberately does not consult the command×
        phase matrix (heat-off is monotonically toward safety in every
        phase; the matrix forbids SET_HEAT in e.g. cooling to prevent
        re-heating, not heat-off). A failed write is surfaced
        (COMMAND_FAILED) but never blocks the fail-closed transition.

        On a write FAILURE the evaluation is LATCHED in ``_pending_fail_safe``
        so the terminal-phase tick re-attempts the heat-off write until it
        confirms — the latch must not strand the roaster hot after one transient
        failure (#206 / PR review blocker). A successful write clears the latch.
        """
        if evaluation.adjusted_heat is None or evaluation.adjusted_fan is None:
            return
        try:
            await self._executor.set_targets(
                heat_percent=evaluation.adjusted_heat,
                fan_percent=evaluation.adjusted_fan,
            )
        except Exception:
            self._events.emit(
                RoastEventKind.COMMAND_FAILED,
                {"command": "set_targets", "context": "fail_safe", "rule": evaluation.rule},
            )
            # Posture NOT confirmed: hold it for retry on the next latched tick.
            self._pending_fail_safe = evaluation
            return
        self._current_heat = evaluation.adjusted_heat
        self._current_fan = evaluation.adjusted_fan
        # Posture confirmed on hardware: nothing left to retry.
        self._pending_fail_safe = None

    def _adopt_applied_state(self, applied: AppliedRoasterState | None) -> None:
        """Adopt a command's actually-applied heat/fan into the commanded mirrors (#507).

        ``drop_beans`` and ``emergency_stop`` change heat/fan/cooling as a
        hardware side effect of the command itself, not through a separate
        ``set_targets`` write — so unlike every other executed command, the
        mirrors (``_current_heat`` / ``_current_fan``) are not already told
        what was requested. Call this ONLY after the executor call has
        already succeeded (mirrors the ``set_targets``-adjacent call sites'
        own "assign the mirror after the await, never before" discipline —
        #412's told==enforced pattern applied to the drop/e-stop path): a
        failed write must never advance what the controller believes is
        commanded.

        ``applied`` is ``None`` when the hardware command succeeded but its
        result payload could not be parsed (a malformed/out-of-contract
        MCP — see ``mcp_client.RoasterControlAdapter``'s
        ``_applied_state_or_none``, which already logged a WARNING). This is
        a genuine no-op, not a failure: the caller has already emitted
        ``COMMAND_EXECUTED`` and transitioned, because the drop/stop DID
        happen — only the mirrors stay at their pre-command values
        (stale-but-honest) rather than adopting a fabricated guess.

        ``cooling_on`` is intentionally NOT mirrored here — the controller has
        no ``_current_cooling`` field; ``cooling_on`` reaches telemetry
        exclusively through the live per-tick MCP read
        (``RoastTelemetry.cooling_on``, see ``mcp_client.project_session_state``),
        which is already correct and unaffected by this bug.

        Args:
            applied: The driver's applied state, as returned by the executor's
                ``drop_beans``/``emergency_stop`` call, or ``None`` on a
                parse failure against an already-successful hardware command.
        """
        if applied is None:
            return
        self._current_heat = applied.heat_level_percent
        self._current_fan = applied.fan_level_percent

    def _heat_off_evaluation(self, *, source_rule: str) -> SafetyEvaluation:
        """A synthetic heat-off / overrun-safe-fan fail-safe target (#206).

        The unconditional fail-closed posture (heat 0 %, the configured
        overrun-safe fan) used as the RETRY target after a failed e-stop and as
        the fallback any time a fail-safe write could not be confirmed. Heat-off
        is monotonically toward safety in every phase, so retrying it can never
        make the roaster less safe.

        Args:
            source_rule: The rule name to stamp on the synthetic evaluation for
                the decision trace (e.g. the e-stop reason source).

        Returns:
            A ``FAULT`` :class:`SafetyEvaluation` carrying heat 0 / safe fan.
        """
        return SafetyEvaluation(
            rule=source_rule,
            verdict=SafetyVerdict.FAULT,
            adjusted_heat=0,
            adjusted_fan=self._safety.limits.overrun_safe_fan_percent,
            reason="fail-closed retry: heat 0 %, overrun-safe fan",
        )

    async def _retry_pending_fail_safe(self) -> None:
        """Re-attempt the latched heat-off write on a terminal-phase tick (#206).

        Called only from the terminal-phase short-circuit in :meth:`tick`. It
        re-issues the held fail-safe write (heat 0 %, safe fan); on success the
        latch clears and later latched ticks are fully silent, on failure it
        stays latched and is retried again next tick. It NEVER re-emits the
        terminal event, never re-reads the MCP, and never re-evaluates safety —
        so it preserves the fail-closed guarantee without re-opening the noise
        loop the latch closed.
        """
        pending = self._pending_fail_safe
        if pending is None:
            return
        await self._apply_fail_safe(pending)

    async def _maybe_escalate_while_latched(self) -> None:
        """Upward-only safety escalation from inside a terminal HOLD phase (#206).

        The latch stops the re-emit/re-read NOISE loop, but a ``faulted`` run is
        also reachable with a still-LIVE MCP (e.g. a stale-telemetry FAULT). If
        that MCP then reports a hard-ceiling breach, the controller must still
        AUTO-escalate to the hardware emergency stop — the automatic upward
        escalation it had before the latch (safety-reviewer carry-forward).

        It re-reads + re-evaluates and acts ONLY when the new verdict is STRICTLY
        MORE SEVERE than the latched one (per :data:`_TERMINAL_VERDICT_SEVERITY`):

        * **Dead/empty read** — hold silently. A raised read or ``None`` is NOT
          counted toward a new fault and emits nothing (the dead-MCP anti-spam
          guarantee: still exactly one FAULT event ever).
        * **Same or lesser verdict** — do nothing: no re-emit, no re-fire.
        * **Strictly more severe** — fire the hardware emergency stop ONCE,
          emit ONE escalation FAULT event, and re-latch at EMERGENCY_STOP. A
          direct foreign-reader non-finite FAULT can out-rank RECOVERY; a hard
          ceiling EMERGENCY_STOP can out-rank FAULT or RECOVERY. Already at
          EMERGENCY_STOP (max severity) ⇒ nothing can out-rank it, so it never
          re-fires.
        """
        latched = self._latched_verdict
        if latched is None:  # pragma: no cover — only entered while latched
            return
        if self._fault_acknowledged:
            # #332: the operator has acknowledged — the run finalises this tick and
            # the loop stops. Heat is already off (the fault forced it, and the
            # heat-off retry above still runs), so there is nothing left to escalate
            # INTO. Skip the re-read so a wedged-child read can't sit between the
            # acknowledge and the finalise (the roast-3 "slow to clear" latency).
            return
        if latched is SafetyVerdict.EMERGENCY_STOP:
            # Already at the top of the severity order — nothing can escalate it,
            # so it never re-reads or re-fires.
            return
        # A read that raises (dead MCP) or returns no session must hold SILENTLY:
        # it must not count toward a new fault or re-emit (anti-spam). So this
        # bypasses _read_telemetry's failure counter deliberately.
        try:
            telemetry = await self._reader.read_telemetry()
        except Exception:
            return
        if telemetry is None:
            return
        evaluation = self._safety.evaluate_telemetry(
            phase=self._phase,
            bean_temp_c=telemetry.bean_temp_c,
            env_temp_c=telemetry.env_temp_c,
            t0_confirmed=self._t0_confirmed,
        )
        new_severity = _TERMINAL_VERDICT_SEVERITY.get(evaluation.verdict)
        if new_severity is None or new_severity <= _TERMINAL_VERDICT_SEVERITY[latched]:
            # ALLOW/CLAMP/REJECT, or a same/lesser terminal verdict: hold, no
            # re-emit, no re-fire — the latch stays at its current level.
            return
        # Strictly more severe → escalate. A foreign reader can supply a
        # non-finite-telemetry FAULT that out-ranks RECOVERY; the live adapter
        # voids that reading to None. Hard-ceiling EMERGENCY_STOP out-ranks both.
        await self._snapshots.persist_evaluation(evaluation)
        await self._escalate_to_emergency_stop(evaluation)

    async def _escalate_to_emergency_stop(self, evaluation: SafetyEvaluation) -> None:
        """Fire the hardware emergency stop ONCE from a latched terminal phase.

        The escalation actuation for :meth:`_maybe_escalate_while_latched`: it
        executes the hardware ``emergency_stop`` (a real upward escalation beyond
        the heat-off fail-safe posture), emits exactly ONE escalation FAULT event,
        and re-latches at ``EMERGENCY_STOP`` so a sustained breach never re-fires.
        A raising e-stop is surfaced and latches a heat-off retry (same fail-closed
        handling as the entry path), never crashing the tick.

        Args:
            evaluation: The hard-ceiling EMERGENCY_STOP evaluation that triggered
                the escalation (already persisted by the caller).
        """
        try:
            applied = await self._executor.emergency_stop(reason=evaluation.reason)
        except Exception:
            self._events.emit(
                RoastEventKind.COMMAND_FAILED,
                {"command": "emergency_stop", "reason": evaluation.reason},
            )
            self._pending_fail_safe = self._heat_off_evaluation(source_rule="emergency_stop_retry")
        else:
            # Confirmed on hardware (#507): adopt the applied heat/fan (see
            # _act_on_safety's identical comment for the full rationale).
            self._adopt_applied_state(applied)
        # An emergency stop lands in FAULTED: escalating from operator_recovery_
        # required (the lower-severity RECOVERY latch) crosses the universal
        # `* → faulted` edge; escalating from a FAULT latch is already there.
        if self._phase is not RoastPhase.FAULTED:
            self.transition_to(RoastPhase.FAULTED)
        # Record the escalation as a single FAULT event and raise the latch to the
        # max verdict so it cannot re-fire on the next identical tick. (Set after
        # the transition: transition_to out of a terminal phase would otherwise
        # clear the latched verdict — but FAULTED is itself terminal, so the
        # leaving-terminal clear does not fire here; setting after is belt-and-
        # braces against that ordering.)
        self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
        self._latched_verdict = SafetyVerdict.EMERGENCY_STOP

    async def _run_advisory(
        self, telemetry: RoastTelemetry | None, trigger: AdvisoryTrigger
    ) -> None:
        """Advisory step: timeout-bounded, never blocks the tick.

        ``trigger`` is why the call-frequency policy fired; it rides along on
        every ADVISORY event for the decision trace. Failure of any kind
        becomes a REJECT evaluation with the deterministic
        hold-current-targets fallback (E3-S3).
        """
        # advisor/profile are already gated in _maybe_run_advisory; kept here
        # for type-narrowing and as a guard if ever called from elsewhere.
        if self._advisor is None or self._profile is None:  # pragma: no cover
            return
        if telemetry is None:
            # Triggered (a manual request, or the heartbeat in a terminal
            # phase) but the sensor read came back empty this tick — the
            # advice phases all fault on missing telemetry before reaching
            # here, so this is the residual manual/terminal case. Surface the
            # skip so the request is visible in the trace, then hold.
            self._events.emit(
                RoastEventKind.ADVISORY,
                {"trigger": trigger.value, "skipped": "no_telemetry"},
            )
            return
        # Command×phase matrix gate (E3-S5/D16) before the advisor is even
        # consulted: set_targets writes heat, so SET_HEAT's row (the
        # stricter of heat/fan) decides whether advice could be applied at
        # all in this phase.
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.SET_HEAT, phase=self._phase
        )
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            await self._snapshots.persist_evaluation(phase_validity)
            self._events.emit(
                RoastEventKind.ADVISORY,
                {"trigger": trigger.value, "evaluation": phase_validity.model_dump(mode="json")},
            )
            return
        # Resolve the phase control box ONCE per advisory cycle (#273/#294):
        # the SAME PhaseControlLimits instance is passed into the advisor
        # context (told) and into evaluate_command (enforced) below, so told ==
        # enforced by structural identity — not by two computations that happen
        # to agree across the advisor await.
        post_fc_fan_signal = self._arm_post_fc_fan_release(self._post_fc_fan_signal(telemetry))
        control_box = self._policy_limits_for(self._phase, post_fc_fan_signal=post_fc_fan_signal)
        context = self._build_advisor_context(telemetry, control_box)
        # The trace identity persisted on every outcome path below (#167),
        # carrying the PHASE-RESOLVED model (#189): the row records the model
        # that actually answered this phase's call, not the base slug — so once
        # the FC/development slot is flipped to a faster model the trace is honest.
        descriptor = self._advisor.descriptor_for(context.phase)
        started = self._clock()
        try:
            decision = await asyncio.wait_for(
                self._advisor.get_recommendation(context),
                timeout=self._config.advisory_timeout_seconds,
            )
        except TimeoutError:
            await self._record_advisor_failure("timeout", trigger, context, started, descriptor)
            return
        except AdvisorMalformedOutputError:
            await self._record_advisor_failure("malformed", trigger, context, started, descriptor)
            return
        except AdvisorUnsafeOutputError:
            await self._record_advisor_failure("unsafe", trigger, context, started, descriptor)
            return
        except Exception:
            await self._record_advisor_failure(
                "provider_error", trigger, context, started, descriptor
            )
            return
        # A reachable advisor that returned a usable decision (``ok``) clears
        # the availability-failure streak (D30, #166): the sustained-outage
        # stop only arms on *consecutive* provider_error/timeout failures, so
        # one good call resets the count. Reset here — before the command
        # verdict — because availability is about reaching the advisor, not
        # whether its advice was then allowed/clamped.
        self._consecutive_advisor_failures = 0
        latency_ms = self._elapsed_ms(started)
        # D40.5 (#275): record the model's OWN recommendation in the decision
        # trace so the NEXT tick's context shows it its trajectory (the #218
        # anti-thrash fix). The trace carries the RECOMMENDED levers (what the
        # model asked for), not the clamped value the gate applies — it is the
        # model's own move history. Context only; no control authority.
        self._history.record_decision(
            DecisionTraceEntry(
                elapsed_since_charge_seconds=self._charge_elapsed_seconds(),
                target_heat=decision.target_heat,
                target_fan=decision.target_fan,
                should_drop=decision.should_drop,
                confidence=decision.confidence,
            )
        )
        if decision.confidence < self._config.post_fc_min_confidence:
            # Fail-closed on low confidence (#276): a model that is unsure must
            # not move the levers — and must not drop. The recommendation is still
            # traced (above) for diagnosis; the no-write REJECT is the outcome
            # attached to it. This is one of the four fail-closed paths (silent /
            # slow / error already returned above; rejected/clamped handled by the
            # gate below). The drop is deliberately NOT evaluated on this path.
            await self._reject_low_confidence(decision, trigger, context, latency_ms, descriptor)
            return
        # #497/#498: shared with :meth:`_build_advisor_context` (via
        # :meth:`_post_fc_loop_active`) so the flag the model was TOLD matches
        # the actuation decision made below by construction, not by two
        # expressions that happen to agree. Hoisted above the safety
        # evaluation (BLOCKER-1 fix, safety-reviewer on #498): in loop mode
        # this consult NEVER writes to the roaster — only the taper's own
        # per-interval write does (see ``_apply_deterministic_post_fc_levers``)
        # — so this evaluation must NOT consume or be gated by the write-
        # cadence rate limit. Two writers sharing one ``min_seconds_between_
        # commands`` slot per tick is exactly BLOCKER-1: the taper (which runs
        # FIRST in ``tick()``'s documented order) would consume the slot on
        # every heat-moving tick, REJECTing the advisor's fan here almost
        # every time it mattered (both cadences default to the same 5 s
        # interval, so same-tick collision was the COMMON case, not an edge
        # case).
        post_fc_loop_active = self._post_fc_loop_active()
        evaluation = self._safety.evaluate_command(
            requested_heat=decision.target_heat,
            requested_fan=decision.target_fan,
            # None in loop mode: this call is a BOUNDS CHECK deriving the
            # desired-fan target for the taper's later write, never a write
            # attempt of its own, so the write-cadence rate limit does not
            # apply to it (baseline mode is unchanged: it IS the write
            # attempt, so it keeps the real elapsed time).
            seconds_since_last_command=(
                None if post_fc_loop_active else self._seconds_since_last_command()
            ),
            # The SAME phase-resolved box instance placed in the advisor context
            # (#273): the model is told this range and the gate clamps to it —
            # told == enforced by identity. #222 narrows pre-FC; D156/D157 may
            # narrow the DEVELOPMENT fan destination on this exact consult box.
            bounds=control_box,
        )
        safety_evaluation_id = await self._snapshots.persist_evaluation(evaluation)
        # Persist the advisor decision and link it to the verdict it produced
        # (#167) — the trace lost the provider/model/decision before this wiring.
        await self._snapshots.persist_advisor_decision(
            descriptor=descriptor,
            context=context,
            latency_ms=latency_ms,
            decision=decision,
            status="ok",
            safety_evaluation_id=safety_evaluation_id,
        )
        self._events.emit(
            RoastEventKind.ADVISORY,
            {
                "trigger": trigger.value,
                "decision": decision.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            },
        )
        # D82/D83 (#405 Slice B2), revised by D89/#498: when the deterministic
        # post-FC RoR loop is ENGAGED (flag on AND this is the post-FC
        # DEVELOPMENT phase AND ``self._post_fc_engaged`` — the pre-FC phases
        # never reach this far, they are gated out of advice entirely), the
        # loop OWNS heat — the advisor's heat recommendation is traced above
        # (ADVISORY event, decision trace, persisted decision) for
        # observability but never actuated, exactly as Slice B2 shipped it.
        #
        # **#498 (D89 Tier 1, division by lever) revises the FAN half of that
        # rule: fan is the advisor's lever in loop mode too** — but (BLOCKER-1
        # fix, safety-reviewer) this branch does NOT write directly. Two
        # writers issuing their own ``set_targets`` every tick (this one, and
        # the taper's own per-interval write in
        # ``_apply_deterministic_post_fc_levers``, which runs FIRST per
        # ``tick()``'s documented order) collided on the single
        # ``min_seconds_between_commands`` rate-limit slot: with both cadences
        # defaulting to 5 s, the taper's heat-moving write consumed the slot
        # on the SAME tick almost every time, REJECTing the advisor's fan
        # write here. The fix coalesces to ONE writer: this branch only
        # updates ``self._post_fc_desired_fan_percent`` — a held TARGET, never
        # itself an MCP write, so it advances regardless of write cadence
        # (the evaluation above used ``seconds_since_last_command=None`` for
        # exactly this reason) — and the taper's single write later applies
        # ``(taper_heat, desired_fan)`` together. Heat and fan are
        # independently clamped fields on one ``SafetyEvaluation`` (#412,
        # safety.py's ``evaluate_command``), so deriving the desired fan from
        # this evaluation's ``adjusted_fan`` needs no second evaluation call.
        #
        # ``_post_fc_engaged`` (safety-review fix, post-B2, MEDIUM): DEVELOPMENT
        # is reachable both via the true FC edge (loop-eligible) and an
        # operator resume out of recovery (NOT loop-eligible — no bumpless
        # seed happened). Without this third condition the loop's phase-only
        # gate would ALSO have suppressed the advisor here on a resume, with
        # NEITHER the loop nor the advisor driving post-FC heat/fan — a silent
        # control gap. Requiring ``_post_fc_engaged`` here means a resume into
        # DEVELOPMENT falls through to the (full-lever, baseline-shaped,
        # directly-actuating) advisor path below exactly as it did before
        # Slice B2 (the pre-B2 fallback), matching the loop's own
        # non-actuation on that edge (``_apply_deterministic_post_fc_levers``).
        if (
            evaluation.verdict in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)
            and evaluation.adjusted_heat is not None
            and evaluation.adjusted_fan is not None
        ):
            if post_fc_loop_active:
                # #498: hold the desired fan for the taper's next write to
                # apply — no write happens here. A stale desired fan is never
                # left behind on disengage/a later engagement: cleared in
                # ``transition_to`` alongside ``_post_fc_engaged`` (D88 C2
                # discipline).
                self._post_fc_desired_fan_percent = evaluation.adjusted_fan
                # Retain provenance only when THIS consult's resolved box proves
                # D156 caused the fan clamp: the ceiling is below the full
                # 0–100 lever, the advisor asked above it, and safety landed on
                # that exact ceiling. Any other accepted consult supersedes the
                # prior request and clears the retention; a failed consult never
                # reaches this branch, so the last successful actor request
                # remains available through an outage.
                doctrine = self._config.ambient_fan_doctrine
                self._post_fc_doctrine_clamped_fan_request_percent = (
                    decision.target_fan
                    if doctrine.enabled
                    and doctrine.post_fc_fan_ceiling_enabled
                    and control_box.fan_ceiling_percent == doctrine.post_fc_fan_ceiling_percent
                    and control_box.fan_ceiling_percent < 100
                    and decision.target_fan > control_box.fan_ceiling_percent
                    and evaluation.adjusted_fan == control_box.fan_ceiling_percent
                    else None
                )
            else:
                await self._execute_advisor_levers(
                    heat=evaluation.adjusted_heat, fan=evaluation.adjusted_fan
                )
        # Compute the SYSTEM development percent ONCE (the #294 compute-once
        # pattern): the same value decides the block, fills the rejection note,
        # and feeds the persisted SafetyEvaluation, so the reported number is
        # exactly the one that blocked — no sub-tick recompute drift.
        system_percent = self._development_percent()
        if decision.should_drop and not self._drop_development_is_coherent(system_percent):
            # Deterministic DROP COHERENCE GUARD (#312). The drop is irreversible,
            # so an advisor ``should_drop=true`` is cross-checked against the
            # SYSTEM's real development percent (_development_percent, charge/FC-
            # referenced) — NOT the model's claimed number, which the first
            # supervised roast showed can be fabricated ("14 %" at a true ~5.4 %).
            # When the system's development is materially below the target window
            # the advisor's drop is REJECTED like a safety verdict: a REJECT
            # SafetyEvaluation is persisted (trace parity with the low-confidence
            # reject, so the blocked drop shows in the safety_evaluations trace,
            # not only the event stream) and the rejection is surfaced as a note,
            # while the same consult's heat/fan advice (applied above) still
            # stands. No roaster write happens, so no invariant is at risk. The
            # operator's manual DROP BEANS is a separate, un-gated operator path;
            # e-stop and the safety box are unaffected.
            #
            # Inside this branch system_percent is non-None by construction:
            # _drop_development_is_coherent returns True (fails open) when the
            # profile is absent or the percent is None, so reaching here guarantees
            # both a loaded profile and a computed development percent.
            assert system_percent is not None  # guaranteed by the guard above
            drop_block = self._safety.evaluate_advisor_drop_coherence(
                system_development_percent=system_percent,
                target_development_percent=self._profile.target_development_percent,
                margin_percent=self._config.drop_dev_margin_percent,
                current_heat=self._current_heat,
                current_fan=self._current_fan,
            )
            await self._snapshots.persist_evaluation(drop_block)
            self._events.emit(
                RoastEventKind.ADVISORY,
                {
                    "drop_rejected": "development_incoherent",
                    "source": "advisor",
                    "system_development_percent": system_percent,
                    "target_development_percent": self._profile.target_development_percent,
                    "drop_dev_margin_percent": self._config.drop_dev_margin_percent,
                },
            )
            return
        if decision.should_drop:
            drop = self._safety.evaluate_drop_recommendation(phase=self._phase)
            await self._snapshots.persist_evaluation(drop)
            # The advisor advice path is reached only in DEVELOPMENT (the sole
            # post-FC advice phase, AUTO_ADVICE_PHASES), and
            # evaluate_drop_recommendation ALLOWs unconditionally in DEVELOPMENT —
            # so the REJECT/false branch here is unreachable today. Kept (not
            # collapsed) because it is the safety boundary if a future phase becomes
            # an advice phase; the un-taken branch is pragma'd, not the guard logic.
            if drop.verdict is SafetyVerdict.ALLOW:  # pragma: no branch — see above
                try:
                    applied = await self._executor.drop_beans()
                except Exception:
                    self._events.emit(
                        RoastEventKind.COMMAND_FAILED,
                        {"command": "drop_beans", "source": "advisor"},
                    )
                    # D96 slice 1.5 (#561): same fail-safe-DOWN clamp as the
                    # two deterministic drop paths (_execute_deterministic_drop)
                    # — a failed advisor drop is exactly the residual PR #560
                    # round 3 identified. `self_healing=False` (Codex round-2
                    # finding #1): the advisor is cadence-gated
                    # (AdvisoryCallPolicy) — there is no guaranteed retry next
                    # tick, so the raise-suppression latch must arm here even
                    # if the clamp's own corrective write also fails.
                    await self._clamp_heat_after_failed_drop(self_healing=False)
                    return
                self._adopt_applied_state(applied)
                self._events.emit(
                    RoastEventKind.COMMAND_EXECUTED,
                    {"command": "drop_beans", "source": "advisor"},
                )
                self.transition_to(RoastPhase.COOLING)

    async def _execute_advisor_levers(self, *, heat: int, fan: int) -> None:
        """Apply a safety-approved advisor heat/fan through the coherence gate (#276).

        The SECOND post-FC gate, after the safety box: the values here have
        already passed :meth:`SafetyPolicy.evaluate_command` (ALLOW/CLAMP), so the
        coherence gate can only ever turn an approved move into a HOLD — it never
        produces a larger or out-of-box value, so it cannot weaken the safety
        verdict. Each lever is judged independently
        (:func:`coherence.evaluate_lever_coherence`): a sub-threshold direction
        REVERSAL versus that lever's last executed move is damped (the #218
        thrash), while a first move, a same-direction move, or a decisive
        (>= threshold) reversal is applied. The damped value holds the current
        lever; the executed direction is recorded for the next consult.

        When BOTH levers are damped to their current values the result is a HOLD —
        no MCP write is issued and no COMMAND_EXECUTED is emitted, so an incoherent
        flip-flop produces no actuation at all. Any executed change emits a
        COHERENCE_DAMPED note for the levers held, for the decision trace.

        **#498 (D89 Tier 1):** this method's sole caller is now the
        BASELINE (post-FC loop NOT active) branch of ``_run_advisory`` — the
        loop-mode branch never calls it. In loop mode the advisor's heat is
        traced but never actuated, and its fan only updates a held desired
        target that the deterministic taper's own single write later applies
        (see :meth:`_apply_deterministic_post_fc_levers`); this method is
        byte-for-byte its pre-#498 baseline-only behavior.

        Args:
            heat: The safety-approved heat percent (the gate may hold it).
            fan: The safety-approved fan percent (the gate may hold it).
        """
        threshold = self._config.post_fc_deadband_threshold_percent
        heat_result = evaluate_lever_coherence(
            requested=heat,
            current=self._current_heat,
            last_direction=self._heat_direction,
            threshold_percent=threshold,
        )
        fan_result = evaluate_lever_coherence(
            requested=fan,
            current=self._current_fan,
            last_direction=self._fan_direction,
            threshold_percent=threshold,
        )
        damped_levers = [
            lever
            for lever, result in (("heat", heat_result), ("fan", fan_result))
            if result.decision is CoherenceDecision.DAMP
        ]
        if damped_levers:
            # Surface every sub-threshold reversal that was suppressed, so the
            # decision trace shows the gate acted (talk/diagnosis material) — even
            # when the other lever still moves.
            self._events.emit(
                RoastEventKind.ADVISORY,
                {
                    "coherence_damped": damped_levers,
                    "requested": {"heat_percent": heat, "fan_percent": fan},
                    "held": {
                        "heat_percent": self._current_heat,
                        "fan_percent": self._current_fan,
                    },
                    "threshold_percent": threshold,
                },
            )
        # Persist the recorded directions for BOTH outcomes, before the hold check.
        # A damped reversal holds the lever value but ADVANCES its direction toward
        # the requested side (#276 Fix 1), so a sustained sub-threshold push
        # converges on the next consult instead of latching the lever forever. The
        # value-holds early-return below must not skip this, or a fully-damped
        # consult would never record the advance. A plain hold (requested ==
        # current) returns the unchanged direction, so this is a no-op there.
        self._heat_direction = heat_result.direction
        self._fan_direction = fan_result.direction
        if (
            heat_result.applied_value == self._current_heat
            and fan_result.applied_value == self._current_fan
        ):
            # Both levers hold (no requested change, or every change damped): a
            # deterministic HOLD — no write, no rate-limit churn. Directions were
            # already recorded above so a sustained damped push still converges.
            return
        await self._executor.set_targets(
            heat_percent=heat_result.applied_value,
            fan_percent=fan_result.applied_value,
        )
        self._current_heat = heat_result.applied_value
        self._current_fan = fan_result.applied_value
        self._last_command_monotonic = self._clock()
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED,
            {
                "heat_percent": heat_result.applied_value,
                "fan_percent": fan_result.applied_value,
            },
        )

    async def _reject_low_confidence(
        self,
        decision: RoastDecision,
        trigger: AdvisoryTrigger,
        context: AdvisorContext,
        latency_ms: int | None,
        descriptor: AdvisorDescriptor,
    ) -> None:
        """Fail closed on a below-floor-confidence post-FC recommendation (#276).

        Produces the no-write REJECT verdict
        (:meth:`SafetyPolicy.evaluate_advisor_low_confidence`) holding the current
        targets, persists it and the (already-traced) decision linked to it, and
        emits the advisory outcome. No lever write, no drop — a model that is
        unsure holds. The drop is deliberately not evaluated: a low-confidence
        drop is the most dangerous self-contradiction the gate exists to stop.

        Args:
            decision: The advisor recommendation (persisted for diagnosis).
            trigger: Why the advisor was consulted this tick.
            context: The context the advisor answered.
            latency_ms: The call latency.
            descriptor: The advisor trace identity.
        """
        evaluation = self._safety.evaluate_advisor_low_confidence(
            confidence=decision.confidence,
            min_confidence=self._config.post_fc_min_confidence,
            current_heat=self._current_heat,
            current_fan=self._current_fan,
        )
        safety_evaluation_id = await self._snapshots.persist_evaluation(evaluation)
        await self._snapshots.persist_advisor_decision(
            descriptor=descriptor,
            context=context,
            latency_ms=latency_ms,
            decision=decision,
            status="ok",
            safety_evaluation_id=safety_evaluation_id,
        )
        self._events.emit(
            RoastEventKind.ADVISORY,
            {
                "trigger": trigger.value,
                "decision": decision.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            },
        )

    async def _record_advisor_failure(
        self,
        status: Literal["timeout", "malformed", "unsafe", "provider_error"],
        trigger: AdvisoryTrigger,
        context: AdvisorContext,
        started: float,
        descriptor: AdvisorDescriptor,
    ) -> None:
        latency_ms = self._elapsed_ms(started)
        # D30 (#166): only the *availability* failures (provider_error /
        # timeout) accrue toward the sustained-outage stop. malformed / unsafe
        # are provider-*reachable* (the model misbehaved, a different class) —
        # they keep the unchanged hold-current REJECT and never touch the
        # counter, so they neither trip nor reset the availability streak.
        if status in ("timeout", "provider_error"):
            self._consecutive_advisor_failures += 1
            evaluation = self._safety.evaluate_advisor_availability(
                consecutive_failures=self._consecutive_advisor_failures,
                current_heat=self._current_heat,
                current_fan=self._current_fan,
            )
        else:
            evaluation = self._safety.evaluate_advisor_failure(
                status=status,
                current_heat=self._current_heat,
                current_fan=self._current_fan,
            )
        safety_evaluation_id = await self._snapshots.persist_evaluation(evaluation)
        # Persist the failed advisor outcome with no decision, linked to the
        # verdict it produced (#167) — the #134 roast lost every failure's
        # provider/model/status this way. ``unsafe`` (out-of-bounds output) has
        # no distinct trace status; it stores as ``malformed`` (a validation
        # failure), while the safety evaluation keeps its own rule for the
        # verdict stream.
        stored_status: AdvisorTraceStatus = "malformed" if status == "unsafe" else status
        await self._snapshots.persist_advisor_decision(
            descriptor=descriptor,
            context=context,
            latency_ms=latency_ms,
            decision=None,
            status=stored_status,
            safety_evaluation_id=safety_evaluation_id,
        )
        self._events.emit(
            RoastEventKind.ADVISORY,
            {"trigger": trigger.value, "evaluation": evaluation.model_dump(mode="json")},
        )
        # A sustained-outage RECOVERY verdict fails closed through the same
        # safety-action path as the telemetry-stage rules (D30, #166): drive
        # heat 0 % / safe fan and enter operator_recovery_required. This never
        # auto-resumes heat/fan — the operator must explicitly resume / drop /
        # cool (the architecture invariant, preserved). Routing through
        # _act_on_safety reuses the recovery machinery; it acts on (does not
        # re-persist) the evaluation already persisted above.
        if evaluation.verdict is SafetyVerdict.RECOVERY:
            # The stop-tick return is deliberately discarded: this is already
            # the last step of the advisory path (the caller returns straight
            # after), so there is no remaining tick work to short-circuit.
            _ = await self._act_on_safety(evaluation)

    def _elapsed_ms(self, started: float) -> int:
        """Milliseconds elapsed since ``started`` on the controller clock."""
        return max(0, int((self._clock() - started) * 1000))

    def load_profile(self, profile: RoastProfile) -> None:
        """Set the active roast profile (test/setup surface; start_run is
        the real run entry point)."""
        self._profile = profile
        if self._run_started_monotonic is None:
            self._run_started_monotonic = self._clock()

    # --- E4-S4: run lifecycle and operator actions ---

    async def start_run(
        self, profile: RoastProfile, *, recording_roast_num: int | None = None
    ) -> None:
        """Start a new roast run (E4-S4).

        Serialized by the transition table: legal only from ``idle``, so a
        second call while a start is in flight (or any run is active)
        raises InvalidTransitionError before any MCP command — the API 409
        is the outer guard, this is the inner one (E3-S5 carry-forward).
        A failed start_session lands in ``faulted`` (operator acks → idle),
        never a half-started run.

        Args:
            profile: The roast profile to freeze for this run.
            recording_roast_num: The store-derived per-origin recording roast
                number (#385), passed by the API layer (prior completed roasts of
                this origin + 1). When ``None`` (a direct caller / a count
                failure), the controller falls back to its per-process counter so
                recording naming is always best-effort and never blocks the roast.
        """
        self.transition_to(RoastPhase.STARTING)  # raises unless idle
        self._profile = profile
        self._run_started_monotonic = self._clock()
        self._current_heat = 0
        self._current_fan = 0
        self._last_telemetry = None  # never carry a prior run's reading into this run
        self._last_command_monotonic = None  # new run: rate-limit baseline resets
        # New run: advisory baselines reset too, so the first consult in the
        # new roast fires on its own merits, not on a previous run's timer.
        self._advisory_policy = AdvisoryCallPolicy(self._config)
        self._consecutive_advisor_failures = 0  # new run: availability streak resets (D30)
        # v0.1.9 recording metadata (#176): derive an origin slug from the bean
        # profile + a per-origin roast number, and hand them to start_session so
        # set_recording_metadata fires BEFORE start_roast_session (the MCP applies
        # the filename only if metadata precedes the session). Skipped when the
        # profile yields no slug — the MCP then falls back safely.
        #
        # #385: the roast number is the STORE-DERIVED per-origin count the API
        # passes (stable + meaningful across restarts). The per-process counter
        # still advances every run as the fallback when no store-derived number is
        # supplied (a direct caller, or a best-effort count failure upstream).
        # Recording naming is best-effort and never blocks the roast (the executor
        # swallows + logs a metadata failure).
        recording_origin = recording_origin_slug(profile)
        self._recording_roast_num += 1
        roast_num = (
            recording_roast_num if recording_roast_num is not None else self._recording_roast_num
        )
        # Advance the per-process counter to at least the store-derived value so a
        # fallback run (store failure) can never produce a number below an already-used
        # per-origin recording filename (#385 auggie finding: without this, a fallback
        # after store-derived 4 would use per-process counter 2 → collision).
        if recording_roast_num is not None:
            self._recording_roast_num = max(self._recording_roast_num, recording_roast_num)
        try:
            await self._executor.start_session(
                recording_origin=recording_origin,
                recording_roast_num=(roast_num if recording_origin is not None else None),
            )
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "start_roast_session"})
            self.transition_to(RoastPhase.FAULTED)
            return
        self.transition_to(RoastPhase.PREHEATING)
        self._events.emit(RoastEventKind.RUN_STARTED, {"profile": profile.name})
        # Initial heat/fan per profile, through safety policy (runtime
        # flow step 5) — never raw.
        # Carry-forward A (#222, #273 PR #294 review): pass the single-source
        # PREHEATING control box as bounds=. With #222 narrowing the pre-FC phases
        # (heat floor 100, fan ceiling ~30) the run-start command must be clamped
        # to the SAME box the policy resolves and the deterministic lever step
        # uses — otherwise run-start would enforce the full 0–100 box while the
        # policy/told side is the narrowed one (a told≠enforced gap at the very
        # first roast command). ``transition_to(PREHEATING)`` ran above, so the
        # current phase is PREHEATING and ``_control_limits`` resolves its box.
        evaluation = self._safety.evaluate_command(
            requested_heat=profile.initial_heat_percent,
            requested_fan=profile.initial_fan_percent,
            seconds_since_last_command=self._seconds_since_last_command(),
            bounds=self._control_limits(),
        )
        await self._snapshots.persist_evaluation(evaluation)
        await self._execute_targets(evaluation)

    async def _execute_targets(self, evaluation: SafetyEvaluation) -> bool:
        """Execute an ALLOW/CLAMP heat/fan evaluation; surface failures.

        Returns:
            ``True`` only when the write actually reached the roaster — i.e.
            ``evaluation`` was ALLOW/CLAMP with both adjusted values present,
            AND ``self._executor.set_targets`` completed without raising (so
            ``self._current_heat``/``self._current_fan``/
            ``self._last_command_monotonic`` were updated to match). ``False``
            on the not-executable verdict (REJECT/RECOVERY/FAULT/
            EMERGENCY_STOP, or a missing adjusted value) AND ``False`` on a
            transient actuator failure (``set_targets`` raised) — in BOTH
            cases the roaster's real heat/fan is unchanged from before this
            call. Callers that hold their own control-loop state keyed on
            "did this write actually land" (e.g.
            :meth:`_apply_deterministic_post_fc_levers`'s PI integrator/EMA,
            :meth:`_apply_deterministic_pre_fc_levers`'s ``_trim_depth_applied``)
            MUST advance that state on this return value, never on
            ``evaluation.verdict`` alone — the #412 told==enforced rule
            extended to the actuator-failure case: an ALLOW verdict whose
            ``set_targets`` call then raises is NOT an executed command, and
            state that advances anyway is a phantom advance (the roaster
            never actually moved).
        """
        if (
            evaluation.verdict not in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)
            or evaluation.adjusted_heat is None
            or evaluation.adjusted_fan is None
        ):
            return False
        try:
            await self._executor.set_targets(
                heat_percent=evaluation.adjusted_heat,
                fan_percent=evaluation.adjusted_fan,
            )
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "set_targets"})
            return False
        self._current_heat = evaluation.adjusted_heat
        self._current_fan = evaluation.adjusted_fan
        self._last_command_monotonic = self._clock()
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED,
            {"heat_percent": evaluation.adjusted_heat, "fan_percent": evaluation.adjusted_fan},
        )
        return True

    def restore_charge_clock(self, t0_detected_at_utc: str) -> None:
        """Restore the charge-referenced DTR clock after a restart (#235).

        The charge clock (``_charge_monotonic``) lives in the process-local
        ``time.monotonic`` reference, which resets on restart, so it cannot be
        persisted directly. Instead the store persists the *absolute* UTC instant
        of the debounced T0 transition; this reconstructs the monotonic anchor
        from it as ``now_monotonic - (now_utc - t0_detected_at)``, so
        :meth:`_charge_elapsed_seconds` (the advisor's DTR denominator, #219)
        reads the true seconds-since-charge again instead of resetting to ``0.0``
        for the rest of the resumed run.

        Mostly advisory/display: ``_charge_monotonic`` feeds the advisor context
        and the operator DTR readout, and through :meth:`_development_percent` it
        is the denominator of the #313 advisor-drop-coherence guard (#337) — so a
        bad restore could shift that guard's release point, but it fails safe: the
        guard only ever HOLDS a drop (no roaster write), never forces one, and it
        gates only the advisor drop path — never a phase transition, the operator
        manual drop, the safety box, e-stop, or any hardware write. The worst case
        of a bad restore is therefore a conservative advisory/guard miss, never an
        unsafe drop. It does **not** re-stamp the charge guidance or the
        post-charge settle window; those are handled by ``operator_resume``.

        A clock skew that would place charge in the future (a negative elapsed)
        is clamped to "charge now" (elapsed 0) rather than fabricating a
        future-referenced clock. A bad stored value is ignored (the clock stays
        ``None`` — the conservative path): both a non-ISO string (``ValueError``)
        and a timezone-NAIVE one (a valid ISO string that parses fine, then
        raises ``TypeError`` on the aware-minus-naive subtraction). Production
        only ever writes ``+00:00`` (``_utc_now``), but recovery must never crash
        on a malformed persisted value.

        Args:
            t0_detected_at_utc: ISO-8601 UTC timestamp of the persisted charge/T0
                detection (``PersistedRun.t0_detected_at_utc``).
        """
        try:
            charged_at = datetime.fromisoformat(t0_detected_at_utc)
            elapsed = (datetime.now(UTC) - charged_at).total_seconds()
        except (ValueError, TypeError):
            return  # malformed/naive persisted value: stay conservative (clock None)
        if elapsed < 0.0:
            elapsed = 0.0  # clock skew: never fabricate a future charge instant
        self._charge_monotonic = self._clock() - elapsed

    async def recover_from_restart(self, persisted_phase: RoastPhase | None) -> None:
        """Startup classification (orchestration plan § Persistence).

        A possibly-active persisted run (anything but none/idle/complete)
        lands in ``operator_recovery_required``: heat and fan are never
        auto-resumed, no MCP write is issued, and emergency stop stays
        available. Explicit operator action is required to resume, drop,
        cool, or end the run.
        """
        if persisted_phase in (None, RoastPhase.IDLE, RoastPhase.COMPLETE):
            return  # nothing possibly active: stay idle
        evaluation = SafetyEvaluation(
            rule="restart_recovery",
            verdict=SafetyVerdict.RECOVERY,
            reason=(
                f"restart with persisted phase {persisted_phase.value}: operator recovery "
                f"required — heat/fan deliberately not resumed"
            ),
        )
        await self._snapshots.persist_evaluation(evaluation)
        self.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
        self._events.emit(RoastEventKind.RECOVERY_REQUIRED, evaluation.model_dump(mode="json"))

    async def recover_into_faulted(self, persisted_phase: RoastPhase | None) -> None:
        """Restart classification for a persisted **faulted** run (#206).

        A hard fault (or e-stop) before the restart must NOT offer
        resume-into-roasting — that would be re-applying heat into an aborted
        run. Such a run re-enters the *operable-faulted* state instead of
        ``operator_recovery_required``: the loop stays alive, heat/fan are NOT
        auto-resumed (``faulted`` is heat-off), emergency stop stays available,
        and the operator may still engage/stop cooling on a physically-running
        machine and then acknowledge the fault to finalise it.

        Mirrors :meth:`recover_from_restart` for the fault case: no MCP write is
        issued and no resume-into-roast edge exists out of ``faulted`` (the
        ``FAULTED -> {IDLE}`` transition row is unchanged). Active-roast phases
        still route through :meth:`recover_from_restart` to recovery.

        Args:
            persisted_phase: The ``agent_phase`` read back from the store. Only
                ``faulted`` re-enters the operable-faulted state here; any other
                value is a no-op (the caller routes it to
                :meth:`recover_from_restart`).
        """
        if persisted_phase is not RoastPhase.FAULTED:
            return  # not a persisted fault: caller handles via recover_from_restart
        evaluation = SafetyEvaluation(
            rule="restart_recovery",
            verdict=SafetyVerdict.FAULT,
            reason=(
                "restart with persisted phase faulted: re-entering operable-faulted — "
                "heat/fan deliberately not resumed; cooling/e-stop remain available until "
                "the operator acknowledges the fault"
            ),
        )
        await self._snapshots.persist_evaluation(evaluation)
        if self._phase is not RoastPhase.FAULTED:
            self.transition_to(RoastPhase.FAULTED)
        self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))

    def operator_resume(self, target: RoastPhase) -> None:
        """Explicit operator resume out of recovery (the operator gate the
        E4-S1 resume edges require). Heat stays at 0 after a resume until
        separately commanded — this method never writes hardware."""
        if self._phase is not RoastPhase.OPERATOR_RECOVERY_REQUIRED:
            raise InvalidTransitionError(self._phase, target)
        # The operator has dealt with the recovery state (e.g. a sustained
        # advisor outage, D30): clear the availability streak so a resumed
        # roast starts the counter fresh rather than re-tripping on the next
        # single failure.
        self._consecutive_advisor_failures = 0
        self.transition_to(target)  # table gates targets; starting is never legal
        # No post-charge settle re-arm under D35 (#222): the advisor is not
        # consulted pre-FC, so a resume into ROASTING_PRE_FIRST_CRACK has no
        # automatic pre-FC consult to suppress (#209 retired). A resume into a
        # pre-FC phase re-engages the DETERMINISTIC lever policy on the next
        # tick — that is the operator's explicit choice (this method IS the
        # explicit operator action), never an auto-resume of heat/fan.
        self._events.emit(RoastEventKind.RECOVERY_ACKNOWLEDGED, {"resumed_to": target.value})

    def operator_acknowledge_fault(self) -> None:
        """Operator acknowledgement ends a faulted run (plan §3).

        Also the reset path from ``complete`` → ``idle`` for the next run
        (the transition table permits both sources).
        """
        prior = self._phase
        self.transition_to(RoastPhase.IDLE)  # legal only from faulted/complete
        # Payload carries the actual prior phase so consumers (E7/E9) can
        # distinguish fault acknowledgement from a normal-completion reset
        # without inspecting event history (review observation, E4-S4 PR).
        self._events.emit(RoastEventKind.RECOVERY_ACKNOWLEDGED, {"acknowledged": prior.value})

    def note_fault_acknowledged(self) -> None:
        """Record that the operator has acknowledged the current fault (#332).

        The runner's ``_dispatch_acknowledge_fault`` calls this on the drain that
        flips its own ``_fault_acknowledged`` flag, so the SAME tick's latched
        ``tick()`` (which runs after the drain) skips the upward-escalation re-read
        (:meth:`_maybe_escalate_while_latched`): the run is finalising this tick and
        heat is already off, so a wedged-child read there would only delay the
        acknowledge from clearing. Pure flag set — issues no hardware write and does
        NOT transition (the runner finalises via ``_handle_completion``, the #206
        operable-faulted design). Cleared on a new run/preheat. The heat-off retry
        latch is untouched, so the fail-closed posture is unchanged.
        """
        self._fault_acknowledged = True

    async def operator_mark_first_crack(self) -> None:
        """Operator FC override: matrix- and source-validated, then relayed
        to MCP with the true OPERATOR source (E3-S5/D16)."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.MARK_FIRST_CRACK, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        source = self._safety.evaluate_event_source(
            transition="first_crack", source=RoastEventSource.OPERATOR
        )
        await self._snapshots.persist_evaluation(source)
        if source.verdict is not SafetyVerdict.ALLOW:
            return
        if not self.can_transition(RoastPhase.DEVELOPMENT):
            raise InvalidTransitionError(self._phase, RoastPhase.DEVELOPMENT)
        try:
            await self._executor.mark_first_crack()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "mark_first_crack"})
            return
        event_payload: dict[str, object] = {"source": RoastEventSource.OPERATOR.value}
        # #592 / Codex P2: operator FC is a supported server-authored path too.
        # Preserve the last validated bean reading when one exists; never invent
        # a landmark before the controller has consumed telemetry for this run.
        if self._last_telemetry is not None:
            event_payload["bean_temp_c"] = self._last_telemetry.bean_temp_c
        self._events.emit(RoastEventKind.FIRST_CRACK, event_payload)
        self.transition_to(RoastPhase.DEVELOPMENT)

    async def operator_drop_beans(self) -> None:
        """Operator drop: matrix-validated, executed, then cooling — EXCEPT from
        ``faulted`` (#210).

        From ``roasting_pre_first_crack`` / ``development`` this is the normal (or
        early-abort) drop: it issues the drop and transitions to ``cooling``.
        From ``faulted`` (#210) it is a SAFE-ING action — an e-stop/fault leaves the
        drum hot (heat off but still hot), so the operator must be able to dump the
        beans before they scorch. It issues the drop WITHOUT a phase transition
        (mirroring the #206 ``operator_start_cooling`` / ``operator_stop_cooling``
        faulted pattern): the run stays ``faulted`` until the operator acknowledges
        it, heat stays off (``set_heat`` is never extended to faulted), and nothing
        is auto-resumed (the restart-never-auto-resumes invariant is untouched —
        this writes a single drop, not heat/fan). Whether ``drop_beans`` itself
        engages cooling on the Hottop is the open §3 verification; either way DROP
        adds no cooling here — START COOLING is a separate operator action already
        available from faulted (#206).

        Hardware is never written unless the resulting state is reachable: the
        transition (when one applies) is guarded first, so a write-then-raise can't
        diverge the FSM from the machine (E4-S4 safety rule)."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.DROP_BEANS, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        # Transition to cooling ONLY on the non-faulted drop. From `faulted` (#210
        # safe-ing) the drop issues with no phase change — the run stays faulted
        # until acknowledged. Guard the transition BEFORE writing hardware so a
        # write-then-raise never diverges the FSM (E4-S4 blocker).
        will_transition = self._phase is not RoastPhase.FAULTED
        if will_transition and not self.can_transition(RoastPhase.COOLING):
            raise InvalidTransitionError(self._phase, RoastPhase.COOLING)
        try:
            applied = await self._executor.drop_beans()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "drop_beans"})
            # D96 slice 1.5 (#561), Codex round-1 finding #2: the operator's
            # own drop is the FOURTH drop path, and a transient MCP failure
            # here is the identical fail-safe-down condition the other three
            # exist for — DEVELOPMENT can be left holding D96 recovery-raised
            # heat with no further deterministic drop guaranteed to retry
            # promptly (the operator may not immediately retry either).
            # Scoped to `will_transition` (true only outside FAULTED): from
            # FAULTED heat is ALREADY off (`_apply_fail_safe`) and SET_HEAT
            # is not even in that phase's command-phase-matrix row, so the
            # clamp would be a guaranteed-REJECT no-op there anyway — gating
            # here keeps that intent explicit at the call site rather than
            # relying on the clamp's own internal phase gate, and keeps
            # `_clamp_heat_after_failed_drop`'s existing "DEVELOPMENT is the
            # only phase either caller invokes this from" pragma honest for
            # the ORIGINAL three (policy/advisor) callers, which never fire
            # outside DEVELOPMENT in the first place.
            if will_transition:
                # `self_healing=False` (Codex round-2 finding #1): the
                # operator's drop is one-shot — never automatically retried
                # — so the raise-suppression latch must arm here even if
                # the clamp's own corrective write also fails.
                await self._clamp_heat_after_failed_drop(self_healing=False)
            return
        self._adopt_applied_state(applied)
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "drop_beans", "source": "operator"}
        )
        if will_transition:
            self.transition_to(RoastPhase.COOLING)

    async def operator_stop_cooling(self) -> None:
        """Operator stop-cooling: matrix-validated.

        From ``cooling`` it stops the cooling cycle and **completes** the run.
        From ``faulted`` (#206) it stops the cooling an e-stop/fault engaged
        **without a phase transition**: the faulted run stays faulted (heat off)
        until the operator acknowledges it, so a fault never strands a running
        cooling fan with no way to stop it. (Log export wires in E5/E9 alongside
        the real MCP client.)"""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.STOP_COOLING, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        completes = self._phase is RoastPhase.COOLING
        if completes and not self.can_transition(RoastPhase.COMPLETE):  # pragma: no cover
            raise InvalidTransitionError(self._phase, RoastPhase.COMPLETE)
        try:
            await self._executor.stop_cooling()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "stop_cooling"})
            return
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "stop_cooling", "source": "operator"}
        )
        if completes:
            self.transition_to(RoastPhase.COMPLETE)
            self._events.emit(RoastEventKind.RUN_COMPLETED, {})

    async def operator_emergency_stop(self, reason: str | None = None) -> None:
        """Operator e-stop: always available, from every phase (E3-S4)."""
        evaluation = self._safety.evaluate_emergency_stop(phase=self._phase, operator_reason=reason)
        await self._snapshots.persist_evaluation(evaluation)
        await self._act_on_safety(evaluation)

    async def operator_mark_beans_added(self) -> None:
        """Operator manual beans-added — the manual-T0 fallback (D19, E9).

        Matrix-validated (``MARK_BEANS_ADDED`` is valid only in ``preheating``)
        then relayed to MCP. No phase transition: the ``preheating`` →
        ``roasting_pre_first_crack`` move still goes through the debounced T0
        path in :meth:`_apply_phase_rules`; this records the charge with MCP so
        its detection logic can proceed."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.MARK_BEANS_ADDED, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        try:
            await self._executor.mark_beans_added()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "mark_beans_added"})
            return
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "mark_beans_added", "source": "operator"}
        )

    async def operator_start_cooling(self) -> None:
        """Operator start-cooling — recovery action / post-drop fallback (D19, E9).

        Matrix-validated (``START_COOLING`` is valid in ``cooling``,
        ``operator_recovery_required`` *or* ``faulted``) then executed. The phases
        are not symmetric: from ``cooling`` it is the post-drop fallback when the
        roaster never reported ``cooling_on`` (no transition, already cooling);
        from ``operator_recovery_required`` it is the operator's recovery resume
        into ``cooling`` (transitions); from ``faulted`` (#206) it engages cooling
        on a hot faulted machine **without a transition** (the run stays faulted,
        heat off, until acknowledged). Hardware is never written unless the
        resulting state is reachable (E4-S4 safety rule)."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.START_COOLING, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        # Transition only on the recovery-resume case. From `cooling` (post-drop
        # fallback) and `faulted` (#206 safe-ing) the command issues with no phase
        # change — a faulted run stays faulted until the operator acknowledges it.
        will_transition = self._phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
        if will_transition and not self.can_transition(  # pragma: no cover — defensive
            RoastPhase.COOLING
        ):
            # Unreachable given the matrix: the only phase that sets
            # will_transition is operator_recovery_required, whose recovery row
            # includes cooling. Mirrors operator_drop_beans' write-safety guard.
            raise InvalidTransitionError(self._phase, RoastPhase.COOLING)
        try:
            await self._executor.start_cooling()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "start_cooling"})
            return
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "start_cooling", "source": "operator"}
        )
        if will_transition:
            self.transition_to(RoastPhase.COOLING)

    def operator_pause_advisory(self) -> None:
        """Operator pauses advisory consults (D19, E9).

        A pure control toggle: no MCP write and no safety evaluation (it cannot
        actuate hardware — the advisor never controls the roaster). Surfaced as
        an ADVISORY event so the decision trace records that advice was paused.
        Every other tick rule — safety, phase transitions, the queue drain —
        keeps running."""
        self._advisory_paused = True
        self._events.emit(RoastEventKind.ADVISORY, {"advisory_paused": True})

    def operator_resume_advisory(self) -> None:
        """Operator resumes advisory consults (D19, E9). The mirror of
        :meth:`operator_pause_advisory`; no MCP write, no safety evaluation."""
        self._advisory_paused = False
        self._events.emit(RoastEventKind.ADVISORY, {"advisory_paused": False})

    def _control_limits(self, *, trim_signal: TrimSignal | None = None) -> PhaseControlLimits:
        """Resolve the current phase's control box from the single source (#273).

        Builds the :class:`RoastControlPolicy` from the safety policy's *own*
        limits (``self._safety.limits`` — the same config the gate enforces) and
        the frozen active profile, then resolves the box for the current phase.

        This convenience wrapper is used by deterministic/run-start/corrective
        paths that do not carry the D156 post-FC fan signal. The advisor consult
        instead calls :meth:`_policy_limits_for` directly, resolves its signalled
        box ONCE, and passes that same :class:`PhaseControlLimits` instance to
        context (told) and safety (enforced).

        Args:
            trim_signal: The live bean-temp + FC-ETA for the deterministic
                anticipatory heat trim (#327), or ``None`` to fail closed to the
                flat #222 floor. Only the deterministic pre-FC lever path supplies
                one; the advisor-consult and run-start callers pass ``None`` (the
                trim governs the controller-actuated pre-FC heat, not the post-FC
                advisor box).

        Returns:
            The phase-resolved control box for the controller's current phase.
        """
        return self._policy_limits_for(self._phase, trim_signal=trim_signal)

    def _policy_limits_for(
        self,
        phase: RoastPhase,
        *,
        trim_signal: TrimSignal | None = None,
        post_fc_fan_signal: PostFcFanSignal | None = None,
    ) -> PhaseControlLimits:
        """Resolve the single-source control box for an arbitrary ``phase``.

        Builds the :class:`RoastControlPolicy` from the safety policy's *own*
        limits (``self._safety.limits``), the frozen active profile, and the
        configured deterministic pre-FC levers (#222), then resolves ``phase``.
        :meth:`_control_limits` wraps it for the current phase; ``start_run``
        passes the run-start phase explicitly so the run-start command is told ==
        enforced against the narrowed pre-FC box (carry-forward A, #273 review).

        Args:
            phase: The agent phase to resolve the control box for.
            trim_signal: The live bean-temp + FC-ETA for the anticipatory heat
                trim (#327), or ``None`` to fail closed to the flat floor.
            post_fc_fan_signal: The doctrine-gated DEVELOPMENT fan-ceiling
                signal, supplied only by the advisor consult. ``None`` preserves
                the full fan destination range.

        Returns:
            The phase-resolved :class:`PhaseControlLimits` box.
        """
        return self._policy().limits_for(
            phase,
            trim_signal=trim_signal,
            post_fc_fan_signal=post_fc_fan_signal,
        )

    def _policy(self) -> RoastControlPolicy:
        """Build the single-source :class:`RoastControlPolicy` for this tick (#273).

        From the safety policy's *own* limits (``self._safety.limits`` — the same
        config the gate enforces), the frozen active profile, the configured
        deterministic pre-FC levers (#222), and the configured post-FC control
        (#563 — decides the told bitter ceiling; see
        :meth:`RoastControlPolicy._bitter_ceiling_temp_c`). Constructed fresh and
        side-effect free each call; shared by :meth:`_policy_limits_for` (box
        resolution) and :meth:`_trim_signal` (the #327 trim-window latch arming)
        so neither keeps a second copy of the limit source.

        Returns:
            A :class:`RoastControlPolicy` over the current safety limits, profile,
            and post-FC control config.
        """
        return RoastControlPolicy(
            self._safety.limits,
            self._profile,
            pre_fc_levers=self._config.pre_first_crack_levers,
            post_fc_control=self._config.post_first_crack_control,
            ambient_fan_doctrine=self._config.ambient_fan_doctrine,
        )

    def _trim_signal(self, telemetry: RoastTelemetry | None) -> TrimSignal | None:
        """Build the live anticipatory-trim signal for this tick, or ``None`` (#327).

        Pairs the freshest bean temperature with the #229 predicted-FC ETA, the
        controller's current per-run latch state, and the bean RoR (for adaptive
        trim depth, #386) so :meth:`RoastControlPolicy.limits_for` can decide
        whether the late-Maillard trim is engaged and at what depth. The bean
        temperature is this tick's ``telemetry`` reading when present, else the
        last accumulated curve sample (the FC-ETA is always derived from the
        curve window). Returns ``None`` — fail closed to the flat floor — only
        when neither a live read nor any curve sample exists (the very first ticks
        of a run). A present signal with an unknown (``None``) FC-ETA is equally
        safe: the policy fails a *fresh* engage closed on it.

        The bean RoR is sourced from ``telemetry.bean_ror_c_per_min`` when the
        reading is available (the controller already computes it for the #229
        ETA and for the advisory change trigger); ``None`` when the telemetry
        read failed or the MCP has not yet produced a RoR reading. A ``None`` RoR
        causes :meth:`LateMaillardTrim.depth_for` to fall back to the fixed
        ``trim_heat_percent`` — the adaptive depth's own fail-closed guarantee.

        Pure builder: it reads ``self._trim_latched`` but never arms it — the latch
        is armed in :meth:`_apply_deterministic_pre_fc_levers` (the actuation path),
        so probing the signal never mutates control state.

        Args:
            telemetry: This tick's reading, or ``None`` on a failed/sessionless
                read (the window then keys on the last curve sample).

        Returns:
            The :class:`TrimSignal` for this tick, or ``None`` when no bean
            temperature is available at all.
        """
        if telemetry is not None:
            bean_temp_c = telemetry.bean_temp_c
            bean_ror_c_per_min = telemetry.bean_ror_c_per_min
        else:
            window = self._history.curve_window()
            if not window:
                return None
            bean_temp_c = window[-1].bean_temp_c
            bean_ror_c_per_min = window[-1].bean_ror_c_per_min
        return TrimSignal(
            bean_temp_c=bean_temp_c,
            first_crack_eta_seconds=self._first_crack_eta_seconds(),
            latched=self._trim_latched,
            bean_ror_c_per_min=bean_ror_c_per_min,
        )

    def _arm_trim_latch(self, trim_signal: TrimSignal | None) -> TrimSignal | None:
        """Arm the per-run trim latch when the fresh-engage window first opens (#327).

        The single mutation point for ``self._trim_latched``: once
        :meth:`RoastControlPolicy.trim_window_open` returns ``True`` (a clean
        FC-ETA inside the window AND bean ≥ the late-Maillard floor) the latch
        flips ``False`` → ``True`` and stays set for the rest of the pre-FC phase
        (reset per run/preheat in :meth:`transition_to`). This is the hysteresis
        that stops the trim oscillating: a later noisy ETA bouncing back above the
        window keeps the trim engaged rather than snapping heat back to 100.

        Returns a signal carrying the freshly-armed latch so the SAME tick already
        trims (no one-tick lag). An already-latched or ``None`` signal is returned
        unchanged. ``trim_window_open`` ignores the carried latch, so this never
        arms on a degenerate signal.

        Args:
            trim_signal: This tick's signal (current latch carried), or ``None``.

        Returns:
            The signal to resolve the box from — re-stamped ``latched=True`` on the
            tick the window first opens, otherwise unchanged.
        """
        if trim_signal is None or self._trim_latched:
            return trim_signal
        if not self._policy().trim_window_open(trim_signal):
            return trim_signal
        self._trim_latched = True
        return TrimSignal(
            bean_temp_c=trim_signal.bean_temp_c,
            first_crack_eta_seconds=trim_signal.first_crack_eta_seconds,
            latched=True,
            bean_ror_c_per_min=trim_signal.bean_ror_c_per_min,
        )

    def _post_fc_fan_signal(self, telemetry: RoastTelemetry | None) -> PostFcFanSignal | None:
        """Build the live DEVELOPMENT fan-ceiling signal without mutating state.

        Ambient is resolved through :meth:`_doctrine_ambient`, so a disabled
        doctrine or absent, unplugged, malformed, or stale probe reaches policy
        as ``None`` and cannot clamp. The heat floor comes only from the loop's
        stashed :attr:`PostFcControlOutput.effective_floor_percent`; the command
        box floor is narrowed to actuated heat and would spuriously release on
        every tick. That stash can lag the live loop by up to one control
        interval. The harmful direction — a stashed floor below the live floor,
        keeping the fan ceiling bound — requires D96 recovery entry and
        self-heals on the next accepted loop actuation.

        When the loop is not engaged the floor is ``None``. Policy deliberately
        treats that as heat having no remaining downward authority, so a
        possibly-climbing bean releases the ceiling immediately. Baseline mode
        is therefore materially unprotected by this ceiling in that state: in
        practice enforcement is a loop-mode feature, and the failure direction
        preserves full #498 fan-brake authority.

        Args:
            telemetry: This tick's reading, or ``None`` when unavailable.

        Returns:
            The immutable signal in DEVELOPMENT, otherwise ``None``.
        """
        if telemetry is None or self._phase is not RoastPhase.DEVELOPMENT:
            return None
        ambient_temp_c, _ = self._doctrine_ambient(telemetry)
        output = self._last_post_fc_output
        return PostFcFanSignal(
            ambient_temp_c=ambient_temp_c,
            current_heat_percent=self._current_heat,
            post_fc_heat_floor_percent=(
                output.effective_floor_percent if output is not None else None
            ),
            bean_ror_c_per_min=telemetry.bean_ror_c_per_min,
            released=self._post_fc_fan_ceiling_released,
        )

    def _arm_post_fc_fan_release(self, signal: PostFcFanSignal | None) -> PostFcFanSignal | None:
        """Arm the one-way per-dwell fan-ceiling release when it becomes due.

        This is the single mutation point for
        ``self._post_fc_fan_ceiling_released``. A fresh latching condition sets
        it once; an already-released signal returns without reading RoR again.
        After the ceiling first binds, any signal that would stop it binding
        also arms the release so the told ceiling cannot re-narrow in the same
        dwell. The returned signal is re-stamped on the arming tick so the same
        consult box is freed with zero delay. With the post-FC loop engaged, the
        actuated fan follows on the taper's next coalesced write, bounded by
        ``post_first_crack_control.control_interval_seconds``.

        If D156 previously clamped an advisor fan request, arming restores that
        exact ADVISOR-requested value as the held desire, upward only. This is
        not a lever move invented by the controller: it is an actor-authored fan
        request that the newly full-range box now permits. The method performs
        no roaster write; the taper remains the single writer and evaluates the
        restored target through the normal safety policy.

        Args:
            signal: This tick's immutable signal, or ``None``.

        Returns:
            The signal re-stamped ``released=True`` when first armed, otherwise
            unchanged.
        """
        if signal is None or signal.released:
            return signal
        policy = self._policy()
        if not policy.post_fc_fan_ceiling_enabled():
            return signal
        ceiling_engaged = policy.post_fc_fan_ceiling_engaged(signal)
        if ceiling_engaged:
            self._log_post_fc_fan_ceiling_once(action="engaged", signal=signal)
            self._post_fc_fan_ceiling_engaged_once = True
            return signal
        if not (self._post_fc_fan_ceiling_engaged_once or policy.fan_ceiling_release_due(signal)):
            return signal
        self._post_fc_fan_ceiling_released = True
        retained_request = self._post_fc_doctrine_clamped_fan_request_percent
        if retained_request is not None and (
            # The None arm is defensive/unreachable; paired state cannot lower fan.
            self._post_fc_desired_fan_percent is None
            or retained_request > self._post_fc_desired_fan_percent
        ):
            self._post_fc_desired_fan_percent = retained_request
        # Keep the latch conservative even if the ceiling never engaged: an
        # unknown brake state must fail toward MORE #498 fan authority, while
        # permitting a later engagement would move toward less. Observability
        # is narrower — only an actual engagement can truthfully be released.
        if self._post_fc_fan_ceiling_engaged_once:
            self._log_post_fc_fan_ceiling_once(action="released", signal=signal)
        return PostFcFanSignal(
            ambient_temp_c=signal.ambient_temp_c,
            current_heat_percent=signal.current_heat_percent,
            post_fc_heat_floor_percent=signal.post_fc_heat_floor_percent,
            bean_ror_c_per_min=signal.bean_ror_c_per_min,
            released=True,
        )

    def _log_post_fc_fan_ceiling_once(
        self, *, action: Literal["engaged", "released"], signal: PostFcFanSignal
    ) -> None:
        """Log one interpretable fan-ceiling transition per DEVELOPMENT dwell.

        Args:
            action: Whether the ceiling first engaged or its latch first released.
            signal: The signal whose values caused that transition.
        """
        if action == "engaged":
            if self._post_fc_fan_ceiling_engage_logged:
                return
            self._post_fc_fan_ceiling_engage_logged = True
        else:
            if self._post_fc_fan_ceiling_release_logged:
                return
            self._post_fc_fan_ceiling_release_logged = True
        ambient = (
            "unavailable" if signal.ambient_temp_c is None else f"{signal.ambient_temp_c:.1f} °C"
        )
        effective_floor = (
            "unknown"
            if signal.post_fc_heat_floor_percent is None
            else f"{signal.post_fc_heat_floor_percent:d} %"
        )
        bean_ror = (
            "unknown"
            if signal.bean_ror_c_per_min is None
            else f"{signal.bean_ror_c_per_min:.1f} °C/min"
        )
        _log.info(
            "D156/D157 post-FC fan ceiling %s: ceiling=%d %%, ambient=%s, "
            "effective_floor=%s, bean_ror=%s. #781",
            action,
            self._config.ambient_fan_doctrine.post_fc_fan_ceiling_percent,
            ambient,
            effective_floor,
            bean_ror,
        )

    def _damp_trim_depth(self, raw_depth: int, trim: LateMaillardTrim) -> int:
        """Compute the deadband + slew-damped trim depth for this tick (#412).

        Pure function: reads ``self._trim_depth_applied`` but does NOT mutate
        it.  The caller (``_apply_deterministic_pre_fc_levers``) advances
        ``_trim_depth_applied`` only after an ACCEPTED write (ALLOW/CLAMP) so
        that a rate-limited REJECT tick does not consume slew budget — a tick
        rejected at the min_seconds_between_commands gate must not shift the
        anchor, otherwise the NEXT accepted tick sees the intermediate value as
        "already committed" and skips the step (#412 Fix 2).

        Two layers of damping (both deterministic, unit-testable):

        - **Slew-rate limit** — the depth can move at most
          ``trim.trim_depth_slew_pp_per_tick`` pp toward the target each
          tick.  A sustained signal change accumulates across accepted ticks
          and arrives in full; a single-tick spike is capped.
        - **Deadband** — after the slew step, if the slew-limited candidate
          still differs from the last applied depth by no more than
          ``trim.trim_depth_deadband_pp`` pp, the old value is kept (no
          write).  This eliminates sub-threshold bounce without hiding real
          moves.

        Order: slew first (limits step size), then deadband (suppresses
        residual jitter on the slew output).  The result is always an integer
        in the caller's ``[min_trim, max_trim]`` range because slew moves
        toward ``raw_depth`` (already clamped) and deadband either holds the
        last value (already in range) or accepts the slew output (also in
        range).

        **Only called when** ``adaptive_depth_enabled`` is ``True`` and the
        trim is engaged — the non-adaptive path is unaffected, preserving the
        default-off byte-for-byte guarantee.

        Args:
            raw_depth: The un-damped adaptive depth from ``depth_for()``,
                already clamped to ``[min_trim, max_trim]``.
            trim: The active ``LateMaillardTrim`` config (coefficients).

        Returns:
            The damped depth candidate for this tick.  The caller is
            responsible for advancing ``_trim_depth_applied`` to this value
            after a successful write.
        """
        prev = self._trim_depth_applied
        if prev is None:
            # First tick in this pre-FC phase: commit unconditionally.
            return raw_depth

        # Slew: move at most slew_pp_per_tick toward raw_depth.
        slew = trim.trim_depth_slew_pp_per_tick
        candidate = max(raw_depth, prev - slew) if raw_depth < prev else min(raw_depth, prev + slew)

        # Deadband: suppress sub-threshold residual jitter.
        if abs(candidate - prev) <= trim.trim_depth_deadband_pp:
            return prev  # hold — caller advances nothing (depth unchanged)

        return candidate

    def _record_curve_history(self, telemetry: RoastTelemetry | None) -> None:
        """Record this tick's curve sample and arm curve milestones (#275).

        Appends the (action, response) curve point — the bean/env temperature
        and RoR the tick read, paired with the heat/fan the controller has
        commanded this tick — to the bounded roast-so-far window, then arms the
        charge-referenced milestones the per-tick context summarises (turning
        point, recovery, drying end). Called late in the tick (after the phase
        rules and deterministic pre-FC levers) so it captures the charge tick and
        the freshly-commanded levers. Pure context assembly: it actuates nothing,
        evaluates no safety, and holds no control authority.

        No-ops before charge (no roast curve yet) and on an empty read (nothing
        to record). First crack is armed on its transition edge (the authoritative
        FC source), not here. Temperatures are Celsius.

        Args:
            telemetry: The reading this tick consumed, or ``None`` on a failed /
                sessionless read.
        """
        if telemetry is None or self._charge_monotonic is None:
            return
        elapsed = self._charge_elapsed_seconds()
        sample = RoastCurveSample(
            elapsed_since_charge_seconds=elapsed,
            bean_temp_c=telemetry.bean_temp_c,
            env_temp_c=telemetry.env_temp_c,
            heat_percent=self._current_heat,
            fan_percent=self._current_fan,
            bean_ror_c_per_min=telemetry.bean_ror_c_per_min,
            env_ror_c_per_min=telemetry.env_ror_c_per_min,
        )
        self._history.record_sample(sample)
        self._arm_pre_fc_milestones(telemetry, elapsed)

    def _arm_pre_fc_milestones(self, telemetry: RoastTelemetry, elapsed: float) -> None:
        """Arm the pre-FC curve milestones from the live reading (#275).

        - TURNING POINT: the post-charge bean-temperature minimum — armed once
          the bean RoR turns from falling to rising (the curve has bottomed out).
          Carried as a DISPLAY-ONLY landmark: #229 found it is a charge-temperature
          proxy (corr 0.979), so it is shown, never used as a control predictor.
          Also emits a :class:`RoastEventKind.TURNING_POINT` SSE event (#409) so
          the live chart marker fires — observability-only (same contract as
          DRYING_END: event + timeline, never an advisor-facing milestone).
        - RECOVERY: the bean RoR at the first reading after the turning point — the
          one turning-point-family metric that survived the #229 confound check
          (a charge-independent early-pace signal), kept cautiously.
        - DRYING END: the drying→browning boundary. NOT armed here as a milestone
          (#229 gives RoR no predictive weight for it, and #351 keeps it out of the
          advisor curve summary by design). It now has an explicit server signal —
          :meth:`_maybe_emit_drying_end` (#351) — which emits it as an SSE event +
          persisted timeline landmark ONLY; deliberately not a ``record_milestone``
          call, so it never enters :attr:`AdvisorContext.roast_milestones`. Do NOT
          add a ``record_milestone(DRYING_END)`` here: that would leak the
          observability signal into the advisor/control path (lead constraint).

        Args:
            telemetry: The reading this tick consumed.
            elapsed: The charge-referenced seconds for this reading.
        """
        if self._first_crack_monotonic is not None:
            # Post-FC: the pre-FC landmarks are already behind us.
            return
        ror = telemetry.bean_ror_c_per_min
        if ror is None:
            return
        if not self._history.has_milestone(RoastMilestoneKind.TURNING_POINT):
            # The turning point is the bean-temp minimum: RoR crosses from
            # negative (post-charge crash) up through zero. Track negative
            # samples as evidence that the dip actually occurred, then arm on
            # the first non-negative RoR that follows a witnessed negative (#409).
            # Without the witness gate, a first post-charge sample that is already
            # ≥0 (noisy RoR / smoothed kernel / very fast recovery) would fire a
            # false user-visible landmark with no real dip in the observed data.
            if ror < 0.0:
                self._seen_negative_ror_after_charge = True
                return
            if ror >= 0.0 and self._seen_negative_ror_after_charge:
                self._history.record_milestone(
                    RoastMilestone(
                        kind=RoastMilestoneKind.TURNING_POINT,
                        elapsed_since_charge_seconds=elapsed,
                        bean_temp_c=telemetry.bean_temp_c,
                    )
                )
                # #409: emit as an SSE event + persisted timeline landmark so
                # the live chart marker fires. Mirrors drying_end (#351):
                # observability-only — NOT a RoastMilestone the advisor reads.
                self._events.emit(
                    RoastEventKind.TURNING_POINT,
                    {
                        "bean_temp_c": telemetry.bean_temp_c,
                        "elapsed_since_charge_seconds": elapsed,
                    },
                )
            return
        if not self._history.has_milestone(RoastMilestoneKind.RECOVERY):
            # The recovery RoR: the bean RoR at the first reading after the
            # turning point (a charge-independent early-pace scalar, #229 KEEP).
            self._history.record_milestone(
                RoastMilestone(
                    kind=RoastMilestoneKind.RECOVERY,
                    elapsed_since_charge_seconds=elapsed,
                    bean_temp_c=telemetry.bean_temp_c,
                    value=ror,
                )
            )

    def _first_crack_eta_seconds(self) -> float | None:
        """The FC-ETA for the current pre-FC curve, or ``None`` (#275 / #229).

        Extrapolates the recent bean RoR toward the configured FC-band target
        (:attr:`ControllerConfig.first_crack_target_bean_temp_c`) — the
        #229-validated anticipation trigger. ``None`` once first crack is detected
        (the development clock is armed) or before there is enough curve to
        project. Context only; never a lever move on its own.
        """
        if self._first_crack_monotonic is not None:
            return None
        return estimate_first_crack_eta_seconds(
            self._history.curve_window(),
            fc_target_bean_temp_c=self._config.first_crack_target_bean_temp_c,
        )

    def _development_time_ratio(self) -> float | None:
        """DTR as a fraction (0-1) for the advisor context (#275).

        The same charge-referenced ratio the operator readout shows as a percent
        (:meth:`_development_percent`), expressed as a fraction for the
        :attr:`AdvisorContext.development_time_ratio` field — a value DISTINCT
        from the development *duration*. ``None`` before first crack. Reuses the
        existing #219/#220 clocks; does not reinvent them.
        """
        percent = self._development_percent()
        if percent is None:
            return None
        return percent / 100.0

    def _post_fc_loop_active(self) -> bool:
        """Whether the deterministic post-FC RoR loop currently OWNS heat (#497/D89).

        ``True`` iff the taper flag is on, the current phase is DEVELOPMENT, and
        this DEVELOPMENT dwell was entered via the true FC edge
        (``self._post_fc_engaged`` — never an operator resume, which leaves the
        loop inert and the advisor driving post-FC heat instead, see
        :meth:`_apply_deterministic_post_fc_levers`). This is the SAME predicate
        :meth:`_run_advisory` already used inline (as ``post_fc_loop_active``)
        to decide whether to actuate the advisor's levers; extracted here so
        :meth:`_build_advisor_context` can tell the model the same thing it
        acts on, rather than recomputing an equivalent expression that could
        drift out of sync (#497 — the advisor was blind to the loop owning
        heat and reasoned from an imagined heat-0).

        Returns:
            ``True`` when the PI loop is actuating DEVELOPMENT heat this tick.
        """
        return (
            self._config.post_first_crack_control.enabled
            and self._phase is RoastPhase.DEVELOPMENT
            and self._post_fc_engaged
        )

    def _doctrine_ambient(self, telemetry: RoastTelemetry) -> tuple[float | None, float | None]:
        """Resolve the ambient pair the #709 fan doctrine may reason on (#732).

        Two gates, in order, both failing toward "absent":

        1. **The doctrine's own flag.** Off (the default) yields ``(None, None)``
           so a roast on the live ``c3`` carries only always-null keys.
        2. **Freshness.** ``c11`` picks a fan regime by comparing the reading
           against ``threshold_c``, so an old reading does not degrade
           gracefully — it seats the model confidently in the wrong regime, and
           at the prompt a stale value looks exactly like a fresh one. Past
           ``max_reading_age_seconds`` (or with the age unknown) the reading is
           declined.

        Declining nulls exactly the two fields a genuinely absent probe already
        nulls, so a stale reading takes the identical absent-ambient path
        ``c11`` was written for — no new teaching, no new branch. The doctrine's
        two numbers stay populated in that case, again exactly as they are for
        an unplugged probe: they describe the doctrine, not the room.

        One shared doctrine-gated value now feeds both the advisor context and
        D156's consult-time destination predicate. It still commands no lever or
        transition itself; stale/disabled input becomes ``None``, which tells the
        advisor ambient is absent and prevents the deterministic ceiling clamp.

        Args:
            telemetry: This tick's telemetry, carrying the live triad and the
                agent-clock age of its ambient reading.

        Returns:
            The ``(ambient_temp_c, ambient_humidity_pct)`` pair to place in the
            advisor context, or ``(None, None)`` when the doctrine is disabled
            or the reading is not fresh enough to reason on.
        """
        doctrine = self._config.ambient_fan_doctrine
        if not doctrine.enabled:
            return None, None
        age_seconds = telemetry.ambient_age_seconds
        # A RANGE check, not an upper bound, and written as ``not (lo <= x <=
        # hi)`` so every way of being invalid fails CLOSED:
        #
        # * ``nan`` — every comparison against it is False, so a bare
        #   ``age > max`` would admit it as fresh.
        # * ``-inf`` and any negative — these satisfy ``x <= max`` and so slip
        #   through an upper bound alone, forwarding an arbitrarily stale
        #   reading. An earlier revision's comment claimed non-finite ages
        #   failed closed; that was true of ``+inf`` and ``nan`` and false of
        #   ``-inf``, which is exactly the kind of half-true claim worth not
        #   leaving in a safety-adjacent comment.
        #
        # The live adapter cannot produce any of them (the age is a difference
        # of one monotonic clock), but ``RoastTelemetry.ambient_age_seconds`` is
        # a public float field with pydantic's default ``allow_inf_nan`` and the
        # code deliberately supports other constructors — replay frames, the
        # offline bake-off harness, a custom ``StateReader``. This is the only
        # inequality in the doctrine path, so it is the only place the fail-open
        # could hide.
        if age_seconds is None or not (0.0 <= age_seconds <= doctrine.max_reading_age_seconds):
            # The latch is spent only on a reading that EXISTS and is untrustworthy.
            # A disabled, unplugged or not-yet-sampled probe also arrives here with
            # ``age_seconds is None``, but nothing went stale — warning there would
            # both mislead (a cadence complaint about a probe that never reported)
            # and, worse, burn the run's one warning so a probe that starts fine and
            # genuinely wedges later goes unreported. That is the case the warning
            # exists for.
            if telemetry.ambient_temp_c is not None:
                self._warn_once_on_ambient_decline(age_seconds)
            return None, None
        return telemetry.ambient_temp_c, telemetry.ambient_humidity_pct

    def _warn_once_on_ambient_decline(self, age_seconds: float | None) -> None:
        """Log the FIRST ambient decline of a run, once (#732).

        The cross-section config validator can only guard what it can see: the
        ``/config`` path, which writes an explicit poll interval. It cannot see
        an interval inherited from the hand-authored MCP yaml, a probe that
        wedges mid-roast, a doctrine retired by the recovery path, or any future
        constructor of :class:`RoastTelemetry`. Every one of those produces the
        same silent outcome — ambient declined on every tick while the dashboard
        Room tile, which reads the ungated telemetry, still shows a
        temperature — and an RP-B arm recorded as "c11 with ambient" while the
        model saw the absent branch throughout.

        Observing the behaviour covers all of them where predicting it cannot,
        which is why this exists alongside the validator rather than instead of
        it. Once per run, because a per-tick log would be noise at 1 Hz and
        would bury the transition that matters.

        Args:
            age_seconds: The declined reading's age, or ``None`` when unknown.
        """
        if self._ambient_decline_warned:
            return
        self._ambient_decline_warned = True
        _log.warning(
            "Ambient declined as stale for the advisor (age=%s, bound=%.1f s); c11 will use "
            "its absent-ambient branch. If this persists, the effective ambient poll cadence "
            "likely exceeds the freshness bound — check the MCP yaml's "
            "ambient.poll_interval_seconds against "
            "controller.ambient_fan_doctrine.max_reading_age_seconds. #732",
            "unknown" if age_seconds is None else f"{age_seconds:.1f} s",
            self._config.ambient_fan_doctrine.max_reading_age_seconds,
        )

    def _build_advisor_context(
        self, telemetry: RoastTelemetry, limits: PhaseControlLimits
    ) -> AdvisorContext:
        """Build the advisor context for the current tick.

        Args:
            telemetry: The latest roaster telemetry snapshot.
            limits: The phase-resolved control box for this advisory cycle. The
                caller passes the SAME instance into the safety gate's
                ``evaluate_command(bounds=...)``, so the box the model is told
                (the context limit fields) is the box the gate enforces — told
                == enforced by identity (#273/#294).

        Returns:
            The populated :class:`AdvisorContext`.
        """
        assert self._profile is not None  # guarded by caller
        doctrine_ambient_temp_c, doctrine_ambient_humidity_pct = self._doctrine_ambient(telemetry)
        return AdvisorContext(
            phase=self._phase,
            # Charge-referenced (#219): the DTR denominator the advisor reasons
            # about (development / time-since-charge), matching the v4 prompt and
            # the bake-off fixtures. NOT the run-referenced snapshot clock the SPA
            # charts — that stays run-referenced (chart x / ROAST TIME unchanged).
            roast_elapsed_seconds=self._charge_elapsed_seconds(),
            development_elapsed_seconds=self._development_elapsed_seconds(),
            current_bean_temp_c=telemetry.bean_temp_c,
            current_env_temp_c=telemetry.env_temp_c,
            bean_ror_c_per_min=telemetry.bean_ror_c_per_min,
            env_ror_c_per_min=telemetry.env_ror_c_per_min,
            target_drop_temp_c=self._profile.target_drop_temp_c,
            target_development_percent=self._profile.target_development_percent,
            charge_guidance_min_c=self._profile.charge_guidance_min_c,
            charge_guidance_max_c=self._profile.charge_guidance_max_c,
            profile_name=self._profile.name,
            first_crack_detected=telemetry.first_crack_detected,
            seconds_since_charge=(
                None if self._charge_monotonic is None else self._clock() - self._charge_monotonic
            ),
            # D35 §8.2/8.3 (#273): the phase-resolved control box the model is
            # told it must reason inside — the SAME limits instance the gate
            # clamps the resulting command into (see the evaluate_command call
            # in _consult_advisor), so told == enforced by identity.
            heat_floor_percent=limits.heat_floor_percent,
            heat_ceiling_percent=limits.heat_ceiling_percent,
            fan_floor_percent=limits.fan_floor_percent,
            fan_ceiling_percent=limits.fan_ceiling_percent,
            bitter_ceiling_temp_c=limits.bitter_ceiling_temp_c,
            emergency_drop_temp_c=limits.emergency_drop_temp_c,
            # D40.3 / D40.5 (#275): the per-tick control-loop context — the
            # bounded roast-so-far curve window + milestone summary, the model's
            # own decision trace (#218), the DTR (distinct from the development
            # duration above), and the validation-supported FC-ETA (#229 KEEP).
            # Read-only context; the controller and safety policy never read it
            # back. Wiring this into the live post-FC consult is #276.
            roast_curve_window=self._history.curve_window(),
            roast_milestones=self._history.milestones(),
            decision_trace=self._history.decision_trace(),
            development_time_ratio=self._development_time_ratio(),
            first_crack_eta_seconds=self._first_crack_eta_seconds(),
            # #497 (D89 Tier 1): the ACTUATED heat/fan, never the advisor's own
            # prior recommendation — ``self._current_heat``/``self._current_fan``
            # are the SAME actuated-output fields the told==enforced safety box
            # is built from (#412), updated only after a write reaches the
            # roaster (evaluation.adjusted_heat/adjusted_fan). Before this the
            # model had no visibility into whether its last recommendation
            # actually landed, so in post-FC loop mode (where the deterministic
            # taper owns heat, #405/D88) it reasoned from an imagined heat-0
            # instead of the taper's real value (evidence: the 11 Jul validation
            # roast, actuated heat pinned at 65 % by the taper, advisor rationale
            # claimed "heat is already at its minimum").
            current_heat_percent=self._current_heat,
            current_fan_percent=self._current_fan,
            # Loop-mode signal: True iff the deterministic post-FC PI loop is
            # actuating DEVELOPMENT heat THIS tick (see
            # :meth:`_post_fc_loop_active`) — the SAME predicate
            # :meth:`_run_advisory` uses to decide whether to actuate the
            # advisor's own heat/fan recommendation. Tells the model its heat
            # number is advisory-only in that case (c1 prompt teaching, #497).
            post_fc_loop_active=self._post_fc_loop_active(),
            # #499 (D89 Tier 1): the acceptable DTR window around the
            # profile's own authoritative target_development_percent, built
            # from the SAME self._config.drop_dev_margin_percent the
            # deterministic drop-coherence guard reads (never a copied
            # constant — told == enforced applied to a margin, #273/#412
            # discipline extended to a tolerance value). roast_style is
            # surfaced as qualitative INTENT ONLY (never its reference
            # numbers) — D84's explicit-wins precedence is unchanged; the c1
            # prompt states this explicitly.
            target_development_percent_min=(
                self._profile.target_development_percent - self._config.drop_dev_margin_percent
            ),
            target_development_percent_max=(
                self._profile.target_development_percent + self._config.drop_dev_margin_percent
            ),
            roast_style=self._profile.roast_style,
            # D96 slice 2 (#559): copied VERBATIM from
            # ``self._last_post_fc_output`` — the SAME ``PostFcControlOutput``
            # ``_apply_deterministic_post_fc_levers`` stashed earlier THIS
            # tick (told == enforced, the #497 precedent) — never re-derived.
            # ``None`` when the loop has not computed anything this
            # engagement yet (pre-FC phases, the loop's flag off, an
            # operator-resume dwell where the loop stays inert, or before its
            # first control tick after the FC edge).
            post_fc_setpoint_c_per_min=(
                None
                if self._last_post_fc_output is None
                else self._last_post_fc_output.setpoint_c_per_min
            ),
            post_fc_heat_authority_state=(
                None
                if self._last_post_fc_output is None
                else self._last_post_fc_output.heat_authority_state
            ),
            # #567 Slice B: the SAME ReferenceRoast the caller retrieved once
            # (fail-soft, flag-gated) before construction and cached on
            # ``self._reference_roast`` — never re-retrieved here or anywhere
            # else in the tick loop. ``None`` (flag off, no qualifying past
            # roast, or a replay session, which pins retrieval off) yields
            # today's exact empty/None fields.
            reference_curve=([] if self._reference_roast is None else self._reference_roast.curve),
            reference_landmarks=(
                None if self._reference_roast is None else self._reference_roast.landmarks
            ),
            # #709 (RP-B): the ambient-aware fan doctrine's context, fed ONLY
            # when the doctrine is explicitly enabled — the ``reference_curve``
            # posture (#567 Slice B). Flag off (the default) leaves all four
            # fields ``None``, so the advisor's prompt JSON gains only
            # always-null keys: a context-SHAPE addition, not a behavioural
            # one. That gate is load-bearing rather than tidy: an always-null
            # key is inert, but a populated, meaningfully-named number is not,
            # so without it every roast on the live default ``c3`` would carry
            # real ambient values and a named fan-step bound into a prompt that
            # never teaches them — changing the live advisor's input and
            # contaminating the very c3 baseline RP-B is measured against.
            #
            # Enabled, the readings are mirrored VERBATIM from THIS tick's
            # telemetry (the live triad the MCP already projects, #464/D86) —
            # not re-read, not cached, not averaged; ``None`` propagates as
            # ``None`` when ambient is uncaptured, disabled, or unavailable.
            # The two doctrine numbers come from the single config group the
            # operator re-fits from RP-D scores, never a second copy in the
            # prompt prose (#218). Read-only throughout: nothing here clamps or
            # gates a lever. A reading too old to trust is declined by
            # ``_doctrine_ambient`` (#732) rather than passed on.
            ambient_temp_c=doctrine_ambient_temp_c,
            ambient_humidity_pct=doctrine_ambient_humidity_pct,
            ambient_fan_threshold_c=(
                self._config.ambient_fan_doctrine.threshold_c
                if self._config.ambient_fan_doctrine.enabled
                else None
            ),
            ambient_fan_step_max_pp=(
                self._config.ambient_fan_doctrine.step_max_pp
                if self._config.ambient_fan_doctrine.enabled
                else None
            ),
        )

    def _seconds_since_last_command(self) -> float | None:
        if self._last_command_monotonic is None:
            return None
        return self._clock() - self._last_command_monotonic
