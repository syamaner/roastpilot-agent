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
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.control_policy import PhaseControlLimits, RoastControlPolicy
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
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
]

Clock = Callable[[], float]
AsyncSleep = Callable[[float], Awaitable[None]]


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


class StateReader(Protocol):
    """Reads the current roast telemetry (E5 wraps get_roast_state)."""

    async def read_telemetry(self) -> RoastTelemetry | None:
        """Return the latest telemetry, or None when no session exists."""
        ...


class CommandExecutor(Protocol):
    """Executes safety-approved roaster writes (E5 wraps the MCP tools)."""

    async def start_session(self) -> None:
        """Start a new MCP roast session."""
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

    async def drop_beans(self) -> None:
        """Drop the beans (normal drop/cooling transition)."""
        ...

    async def start_cooling(self) -> None:
        """Start the cooling cycle (recovery action / post-drop fallback)."""
        ...

    async def stop_cooling(self) -> None:
        """Stop the cooling cycle."""
        ...

    async def emergency_stop(self, *, reason: str) -> None:
        """Fire the MCP emergency_stop command."""
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
_AUTO_ADVICE_PHASES: frozenset[RoastPhase] = _ADVICE_PHASES - _DETERMINISTIC_PRE_FC_PHASES


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
        if phase not in _AUTO_ADVICE_PHASES:
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
    operator sees the live development time + DTR (no client-side derivation)."""

    phase: RoastPhase
    current_heat: int
    current_fan: int
    roast_elapsed_seconds: float
    development_elapsed_seconds: float | None
    development_percent: float | None
    telemetry: RoastTelemetry | None
    advisory_paused: bool
    #: Whether the charge/T0 clock has been stamped (``_charge_monotonic`` set,
    #: #235). The runner persists the absolute charge instant once this first
    #: reads ``True`` so a later restart→resume can restore the DTR clock. A
    #: pure read; advisory/display-only and never safety-gating.
    charge_detected: bool


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
    ) -> None:
        self._config = config
        self._safety = safety
        self._reader = state_reader
        self._executor = command_executor
        self._snapshots = snapshot_sink
        self._events = event_emitter
        self._advisor = advisor
        self._clock = clock
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
        # Monotonic instant of the drop (the transition into COOLING), set in
        # ``transition_to`` and cleared on a new run/preheat (#239). Freezes the
        # development + charge clocks — and so the derived DTR (#220) — at their
        # drop values: post-drop the readout must hold the drop figure, not keep
        # climbing into cooling. Advisory/display-only; clamps no control path.
        self._drop_monotonic: float | None = None
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
        # D35 §4-A / D40.5 (#276): the direction of each lever's last EXECUTED
        # post-FC move, fed into the coherence/deadband gate so a sub-threshold
        # direction reversal (the #218 30<->40<->30 thrash) is damped to a hold.
        # Reset on each new run/preheat (a fresh roast has no prior trajectory).
        # Context for the deterministic gate only — it never bypasses safety.
        self._heat_direction = LeverDirection.NONE
        self._fan_direction = LeverDirection.NONE
        self._last_command_monotonic: float | None = None
        self._t0_streak = 0
        self._t0_confirmed = False
        self._guidance_emitted = False
        self._operator_state_entered: float | None = None
        self._operator_timeout_alerted = False
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
        return ControllerSnapshot(
            phase=self._phase,
            current_heat=self._current_heat,
            current_fan=self._current_fan,
            roast_elapsed_seconds=self._roast_elapsed_seconds(),
            development_elapsed_seconds=self._development_elapsed_seconds(),
            development_percent=self._development_percent(),
            telemetry=self._last_telemetry,
            advisory_paused=self._advisory_paused,
            charge_detected=self._charge_monotonic is not None,
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

        Advisory/display-only: it bounds nothing on the control path — no
        transition, verdict, executor, or drop gate reads these clocks.
        """
        now = self._clock()
        if self._drop_monotonic is None:
            return now
        return min(now, self._drop_monotonic)

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
        if target in (RoastPhase.STARTING, RoastPhase.PREHEATING):
            # Per-run latches reset (T0 confirmation, debounce streak,
            # add-beans guidance) on a new run AND on every preheating
            # entry: a recovery-resume into preheating declares "back
            # before charge", so the pre-T0 overrun guard must re-arm —
            # a stale _t0_confirmed would disarm it (safety review, E4-S3).
            self._t0_streak = 0
            self._t0_confirmed = False
            self._guidance_emitted = False
            # A new run/preheat resets the development clock; it is (re)armed
            # only on the first-crack transition below.
            self._first_crack_monotonic = None
            # A new run/preheat is "back before charge": clear the charge clock
            # so ``seconds_since_charge`` is None (#219). It is restamped at the
            # debounced T0 transition.
            self._charge_monotonic = None
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
            self._first_crack_monotonic = self._clock()
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
        """Run one controller tick in the documented order."""
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
        # D40.3 (#275): accumulate the roast-so-far curve + milestones for the
        # per-tick control-loop context AFTER the phase rules + deterministic
        # pre-FC levers have run, so the sample captures the charge tick itself
        # and pairs the reading with the heat/fan the controller actually
        # commanded this tick (the (action, response) history the model reads).
        # Context assembly only — it actuates nothing and evaluates no safety; a
        # fail-closed tick returns above and records no sample (a faulting roast
        # is not building context for an advisor that will not be consulted).
        self._record_curve_history(telemetry)
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
          rate-limit churn, no redundant serial writes. The constant target means
          steady high heat / low fan; the heat floor == the target, so even a
          spurious lower value would be clamped back up (the #218 70→40→20→0 crash
          is structurally impossible pre-FC).

        Args:
            telemetry: The reading this tick consumed. Unused for the lever value
                (the target is deterministic, not telemetry-derived in M1 — the
                telemetry-driven late-Maillard trim is deferred to D36/#228), but
                taken so the signature matches the tick-pipeline steps and a future
                telemetry-aware refinement needs no plumbing change.
        """
        del telemetry  # deterministic target is not telemetry-derived in M1 (#222)
        if self._profile is None:
            return
        box = self._control_limits()
        if not box.has_deterministic_target:
            return
        # Validated all-or-nothing on PhaseControlLimits: a deterministic target
        # implies both lever values are present.
        assert box.heat_target_percent is not None  # noqa: S101 (validator invariant)
        assert box.fan_target_percent is not None  # noqa: S101 (validator invariant)
        target_heat = box.heat_target_percent
        target_fan = box.fan_target_percent
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
        await self._execute_targets(evaluation)

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
                self._t0_streak = 0  # a failed/absent read breaks the window
                return
            self._maybe_emit_charge_guidance(telemetry)
            if telemetry.t0_detected:
                self._t0_streak += 1
            else:
                self._t0_streak = 0
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
                self._charge_monotonic = self._clock()
            return
        if (
            self._phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
            and telemetry is not None
            and telemetry.first_crack_detected
        ):
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
                await self._executor.emergency_stop(reason=evaluation.reason)
            except Exception:
                # A raising e-stop must not crash the tick loop or leave
                # the phase pre-fault: fault anyway and surface the failed
                # command. Timeout-bounding the call itself is E5's
                # criterion; retry/fallback hardening is E4-S4's.
                self._events.emit(
                    RoastEventKind.COMMAND_FAILED,
                    {"command": "emergency_stop", "reason": evaluation.reason},
                )
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
            self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            return True
        if verdict is SafetyVerdict.FAULT:
            await self._apply_fail_safe(evaluation)
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
            self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            return True
        if verdict is SafetyVerdict.RECOVERY:
            await self._apply_fail_safe(evaluation)
            if self._phase is not RoastPhase.OPERATOR_RECOVERY_REQUIRED:
                self.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
            self._events.emit(RoastEventKind.RECOVERY_REQUIRED, evaluation.model_dump(mode="json"))
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
            return
        self._current_heat = evaluation.adjusted_heat
        self._current_fan = evaluation.adjusted_fan

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
        control_box = self._control_limits()
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
        evaluation = self._safety.evaluate_command(
            requested_heat=decision.target_heat,
            requested_fan=decision.target_fan,
            seconds_since_last_command=self._seconds_since_last_command(),
            # The SAME phase-resolved box instance placed in the advisor context
            # (#273): the model is told this range and the gate clamps to it —
            # told == enforced by identity. Today this is the full 0–100 lever
            # (verdict no-op); #222 narrows it per phase on the same single
            # source.
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
        if (
            evaluation.verdict in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)
            and evaluation.adjusted_heat is not None
            and evaluation.adjusted_fan is not None
        ):
            await self._execute_advisor_levers(
                heat=evaluation.adjusted_heat, fan=evaluation.adjusted_fan
            )
        if decision.should_drop:
            drop = self._safety.evaluate_drop_recommendation(phase=self._phase)
            await self._snapshots.persist_evaluation(drop)
            if drop.verdict is SafetyVerdict.ALLOW:
                try:
                    await self._executor.drop_beans()
                except Exception:
                    self._events.emit(
                        RoastEventKind.COMMAND_FAILED,
                        {"command": "drop_beans", "source": "advisor"},
                    )
                    return
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
        if (
            heat_result.applied_value == self._current_heat
            and fan_result.applied_value == self._current_fan
        ):
            # Both levers hold (no requested change, or every change damped): a
            # deterministic HOLD — no write, no rate-limit churn. The recorded
            # directions are unchanged (a hold seeds no trajectory).
            return
        await self._executor.set_targets(
            heat_percent=heat_result.applied_value,
            fan_percent=fan_result.applied_value,
        )
        self._current_heat = heat_result.applied_value
        self._current_fan = fan_result.applied_value
        self._heat_direction = heat_result.direction
        self._fan_direction = fan_result.direction
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

    async def start_run(self, profile: RoastProfile) -> None:
        """Start a new roast run (E4-S4).

        Serialized by the transition table: legal only from ``idle``, so a
        second call while a start is in flight (or any run is active)
        raises InvalidTransitionError before any MCP command — the API 409
        is the outer guard, this is the inner one (E3-S5 carry-forward).
        A failed start_session lands in ``faulted`` (operator acks → idle),
        never a half-started run.
        """
        self.transition_to(RoastPhase.STARTING)  # raises unless idle
        self._profile = profile
        self._run_started_monotonic = self._clock()
        self._current_heat = 0
        self._current_fan = 0
        self._last_command_monotonic = None  # new run: rate-limit baseline resets
        # New run: advisory baselines reset too, so the first consult in the
        # new roast fires on its own merits, not on a previous run's timer.
        self._advisory_policy = AdvisoryCallPolicy(self._config)
        self._consecutive_advisor_failures = 0  # new run: availability streak resets (D30)
        try:
            await self._executor.start_session()
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

    async def _execute_targets(self, evaluation: SafetyEvaluation) -> None:
        """Execute an ALLOW/CLAMP heat/fan evaluation; surface failures."""
        if (
            evaluation.verdict not in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)
            or evaluation.adjusted_heat is None
            or evaluation.adjusted_fan is None
        ):
            return
        try:
            await self._executor.set_targets(
                heat_percent=evaluation.adjusted_heat,
                fan_percent=evaluation.adjusted_fan,
            )
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "set_targets"})
            return
        self._current_heat = evaluation.adjusted_heat
        self._current_fan = evaluation.adjusted_fan
        self._last_command_monotonic = self._clock()
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED,
            {"heat_percent": evaluation.adjusted_heat, "fan_percent": evaluation.adjusted_fan},
        )

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

        Advisory/display-only: ``_charge_monotonic`` feeds the advisor context
        and the operator DTR readout, never a transition, verdict, or hardware
        write — the worst case of a bad restore is a conservative advisory miss,
        never an unsafe drop. It does **not** re-stamp the charge guidance or the
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
        self._events.emit(RoastEventKind.FIRST_CRACK, {"source": RoastEventSource.OPERATOR.value})
        self.transition_to(RoastPhase.DEVELOPMENT)

    async def operator_drop_beans(self) -> None:
        """Operator drop: matrix-validated, executed, then cooling."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.DROP_BEANS, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        # Never write hardware unless the resulting state is reachable —
        # a write-then-raise would diverge the FSM from the machine
        # (safety review blocker, E4-S4).
        if not self.can_transition(RoastPhase.COOLING):
            raise InvalidTransitionError(self._phase, RoastPhase.COOLING)
        try:
            await self._executor.drop_beans()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "drop_beans"})
            return
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "drop_beans", "source": "operator"}
        )
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

    def _control_limits(self) -> PhaseControlLimits:
        """Resolve the current phase's control box from the single source (#273).

        Builds the :class:`RoastControlPolicy` from the safety policy's *own*
        limits (``self._safety.limits`` — the same config the gate enforces) and
        the frozen active profile, then resolves the box for the current phase.

        The caller (:meth:`_consult_advisor`) resolves this box ONCE per
        advisory cycle and passes that single :class:`PhaseControlLimits`
        instance into both the advisor context (told) and the safety gate
        (enforced), so within a tick the two read the same object and can never
        carry different numbers (D35 §8.3).

        Returns:
            The phase-resolved control box for the controller's current phase.
        """
        return self._policy_limits_for(self._phase)

    def _policy_limits_for(self, phase: RoastPhase) -> PhaseControlLimits:
        """Resolve the single-source control box for an arbitrary ``phase``.

        Builds the :class:`RoastControlPolicy` from the safety policy's *own*
        limits (``self._safety.limits``), the frozen active profile, and the
        configured deterministic pre-FC levers (#222), then resolves ``phase``.
        :meth:`_control_limits` wraps it for the current phase; ``start_run``
        passes the run-start phase explicitly so the run-start command is told ==
        enforced against the narrowed pre-FC box (carry-forward A, #273 review).

        Args:
            phase: The agent phase to resolve the control box for.

        Returns:
            The phase-resolved :class:`PhaseControlLimits` box.
        """
        policy = RoastControlPolicy(
            self._safety.limits,
            self._profile,
            pre_fc_levers=self._config.pre_first_crack_levers,
        )
        return policy.limits_for(phase)

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
        - RECOVERY: the bean RoR at the first reading after the turning point — the
          one turning-point-family metric that survived the #229 confound check
          (a charge-independent early-pace signal), kept cautiously.
        - DRYING END: the drying→browning boundary the phase model would cross; in
          M1 the agent has no separate drying phase, so this is left to a future
          phase signal and not armed from RoR alone (#229 gives it no predictive
          weight). Recorded only when an explicit signal exists.

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
            # negative (post-charge crash) up through zero. Arm on the first
            # non-negative RoR after charge.
            if ror >= 0.0:
                self._history.record_milestone(
                    RoastMilestone(
                        kind=RoastMilestoneKind.TURNING_POINT,
                        elapsed_since_charge_seconds=elapsed,
                        bean_temp_c=telemetry.bean_temp_c,
                    )
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
        )

    def _seconds_since_last_command(self) -> float | None:
        if self._last_command_monotonic is None:
            return None
        return self._clock() - self._last_command_monotonic
