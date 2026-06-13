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
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
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


# Advice is only worth requesting in phases where its output (a heat/fan
# target) could legally execute — the SET_HEAT row of the command×phase
# matrix is the single source of truth, so this never drifts from safety.
_ADVICE_PHASES: frozenset[RoastPhase] = COMMAND_PHASE_MATRIX[RoastCommand.SET_HEAT]


class AdvisoryCallPolicy:
    """Decides when the advisor is consulted (orchestration plan § Advisory
    Call Frequency).

    Change-based, never every tick: an automatic call fires only on a
    meaningful change since the last call — a phase transition, a bean-temp
    move of ``advisory_min_temp_delta_c``, a RoR move of
    ``advisory_min_ror_delta_c_per_min``, or a **phase-keyed** consult-floor
    heartbeat (``advisory_min_interval_seconds`` mapped per agent phase, #171:
    preheat 30 s, beans-charged/pre-FC 10 s, development 0 = unthrottled).
    The interval is a floor only — the change-based triggers fire sooner. A
    manual operator request bypasses every gate, including phase scoping, so
    the operator always gets a response (the command×phase matrix then
    decides whether that advice can apply).

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
        in advice-applicable phases, evaluated most-meaningful first: a phase
        change (including the first consult in an advice phase), then the
        bean-temp and RoR deltas, then the minimum-interval heartbeat.
        """
        if manual_request:
            return AdvisoryTrigger.MANUAL
        if phase not in _ADVICE_PHASES:
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
        # Phase-keyed consult floor (#171). The interval scales with roast
        # criticality: preheat 30 s, charged/pre-FC 10 s, development 0 =
        # unthrottled. A 0 floor makes this condition fire every eligible
        # tick once the prior call has returned (calls are serial, awaited in
        # the tick), so first-crack/development consults run back-to-back at
        # the advisor's own latency — the change-based triggers above still
        # short-circuit sooner in any phase. Resolved from the *current*
        # phase, not the phase at the last call, so the floor follows the
        # roast forward.
        if now - self._last_call_monotonic >= self._config.advisory_interval_for(phase):
            return AdvisoryTrigger.MIN_INTERVAL
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
    until separately commanded — the restart invariant)."""

    phase: RoastPhase
    current_heat: int
    current_fan: int
    roast_elapsed_seconds: float
    telemetry: RoastTelemetry | None
    advisory_paused: bool


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
        self._consecutive_read_failures = 0
        self._advisory_requested = False
        self._advisory_policy = AdvisoryCallPolicy(config)
        self._current_heat = 0
        self._current_fan = 0
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
            telemetry=self._last_telemetry,
            advisory_paused=self._advisory_paused,
        )

    def _roast_elapsed_seconds(self) -> float:
        if self._run_started_monotonic is None:
            return 0.0
        return self._clock() - self._run_started_monotonic

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

    async def _apply_phase_rules(self, telemetry: RoastTelemetry | None) -> None:
        """MCP-driven phase rules: preheating (E4-S3) and roasting (E4-S4).

        Preheating: add-beans guidance is emitted exactly once when bean or
        environment temperature enters the profile's charge guidance band —
        guidance, not a blocking operator-required state. The T0 debounce
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
        if self._guidance_emitted or self._profile is None:
            return
        low = self._profile.charge_guidance_min_c
        high = self._profile.charge_guidance_max_c
        if low <= telemetry.bean_temp_c <= high or low <= telemetry.env_temp_c <= high:
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
        # The trace identity (provider/model/prompt) persisted with the advisor
        # decision on every outcome path below (#167) — captured once, here,
        # where the advisor is narrowed non-None.
        descriptor = self._advisor.descriptor
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
        context = self._build_advisor_context(telemetry)
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
        latency_ms = self._elapsed_ms(started)
        evaluation = self._safety.evaluate_command(
            requested_heat=decision.target_heat,
            requested_fan=decision.target_fan,
            seconds_since_last_command=self._seconds_since_last_command(),
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
            await self._executor.set_targets(
                heat_percent=evaluation.adjusted_heat,
                fan_percent=evaluation.adjusted_fan,
            )
            self._current_heat = evaluation.adjusted_heat
            self._current_fan = evaluation.adjusted_fan
            self._last_command_monotonic = self._clock()
            self._events.emit(
                RoastEventKind.COMMAND_EXECUTED,
                {"heat_percent": evaluation.adjusted_heat, "fan_percent": evaluation.adjusted_fan},
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

    async def _record_advisor_failure(
        self,
        status: Literal["timeout", "malformed", "unsafe", "provider_error"],
        trigger: AdvisoryTrigger,
        context: AdvisorContext,
        started: float,
        descriptor: AdvisorDescriptor,
    ) -> None:
        latency_ms = self._elapsed_ms(started)
        evaluation = self._safety.evaluate_advisor_failure(
            status=status,
            current_heat=self._current_heat,
            current_fan=self._current_fan,
        )
        safety_evaluation_id = await self._snapshots.persist_evaluation(evaluation)
        # Persist the failed advisor outcome with no decision, linked to the
        # REJECT it produced (#167) — the #134 roast lost every failure's
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
        evaluation = self._safety.evaluate_command(
            requested_heat=profile.initial_heat_percent,
            requested_fan=profile.initial_fan_percent,
            seconds_since_last_command=self._seconds_since_last_command(),
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

    def operator_resume(self, target: RoastPhase) -> None:
        """Explicit operator resume out of recovery (the operator gate the
        E4-S1 resume edges require). Heat stays at 0 after a resume until
        separately commanded — this method never writes hardware."""
        if self._phase is not RoastPhase.OPERATOR_RECOVERY_REQUIRED:
            raise InvalidTransitionError(self._phase, target)
        self.transition_to(target)  # table gates targets; starting is never legal
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
        """Operator stop-cooling: matrix-validated; completes the run.
        (Log export wires in E5/E9 alongside the real MCP client.)"""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.STOP_COOLING, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        if not self.can_transition(RoastPhase.COMPLETE):
            raise InvalidTransitionError(self._phase, RoastPhase.COMPLETE)
        try:
            await self._executor.stop_cooling()
        except Exception:
            self._events.emit(RoastEventKind.COMMAND_FAILED, {"command": "stop_cooling"})
            return
        self._events.emit(
            RoastEventKind.COMMAND_EXECUTED, {"command": "stop_cooling", "source": "operator"}
        )
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

        Matrix-validated (``START_COOLING`` is valid in ``cooling`` *or*
        ``operator_recovery_required``) then executed. The two phases are not
        symmetric: from ``cooling`` it is the post-drop fallback when the roaster
        never reported ``cooling_on`` and does **not** transition (already
        cooling); from ``operator_recovery_required`` it is the operator's
        recovery resume into ``cooling`` and transitions. Hardware is never
        written unless the resulting state is reachable (E4-S4 safety rule)."""
        phase_validity = self._safety.evaluate_command_phase(
            command=RoastCommand.START_COOLING, phase=self._phase
        )
        await self._snapshots.persist_evaluation(phase_validity)
        if phase_validity.verdict is not SafetyVerdict.ALLOW:
            return
        will_transition = self._phase is not RoastPhase.COOLING
        if will_transition and not self.can_transition(  # pragma: no cover — defensive
            RoastPhase.COOLING
        ):
            # Unreachable given the matrix: the only non-cooling phase that
            # reaches here is operator_recovery_required, whose recovery row
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

    def _build_advisor_context(self, telemetry: RoastTelemetry) -> AdvisorContext:
        assert self._profile is not None  # guarded by caller
        return AdvisorContext(
            phase=self._phase,
            roast_elapsed_seconds=self._roast_elapsed_seconds(),
            development_elapsed_seconds=None,
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
        )

    def _seconds_since_last_command(self) -> float | None:
        if self._last_command_monotonic is None:
            return None
        return self._clock() - self._last_command_monotonic
