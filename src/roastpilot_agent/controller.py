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
from typing import Literal, Protocol

from roastpilot_agent.advisor import AdvisorContext, RoastAdvisor
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.models import (
    RoastEventKind,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict

__all__ = [
    "TRANSITION_TABLE",
    "UNIVERSAL_TARGETS",
    "CommandExecutor",
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
    # First crack detected or operator override.
    RoastPhase.ROASTING_PRE_FIRST_CRACK: frozenset({RoastPhase.DEVELOPMENT}),
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

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        """Apply validated heat/fan targets."""
        ...

    async def emergency_stop(self, *, reason: str) -> None:
        """Fire the MCP emergency_stop command."""
        ...


class SnapshotSink(Protocol):
    """Persists tick data (E6 implements over SQLite)."""

    async def persist_snapshot(self, telemetry: RoastTelemetry | None) -> None:
        """Persist the raw telemetry snapshot for this tick."""
        ...

    async def persist_evaluation(self, evaluation: SafetyEvaluation) -> None:
        """Persist a safety evaluation."""
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
        self._current_heat = 0
        self._current_fan = 0
        self._last_command_monotonic: float | None = None

    # --- E4-S1: transitions ---

    @property
    def phase(self) -> RoastPhase:
        """Current agent phase."""
        return self._phase

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
        self._events.emit(RoastEventKind.PHASE_CHANGED, {"phase": target.value})

    # --- E4-S2: tick pipeline ---

    def request_advisory(self) -> None:
        """Operator/manual advisory trigger (the change-based call-frequency
        policy lands in E8-S3 and will also set this)."""
        self._advisory_requested = True

    async def tick(self) -> None:
        """Run one controller tick in the documented order."""
        telemetry, read_failed = await self._read_telemetry()
        await self._snapshots.persist_snapshot(telemetry)
        evaluation = self._evaluate_safety(telemetry, read_failed=read_failed)
        await self._snapshots.persist_evaluation(evaluation)
        if await self._act_on_safety(evaluation):
            return  # fail-closed action taken: no advisory, no commands
        if self._advisory_requested:
            await self._run_advisory(telemetry)

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
            t0_confirmed=self._phase is not RoastPhase.PREHEATING,
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
            await self._executor.emergency_stop(reason=evaluation.reason)
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
            self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            return True
        if verdict is SafetyVerdict.FAULT:
            if self._phase is not RoastPhase.FAULTED:
                self.transition_to(RoastPhase.FAULTED)
            self._events.emit(RoastEventKind.FAULT, evaluation.model_dump(mode="json"))
            return True
        if verdict is SafetyVerdict.RECOVERY:
            if self._phase is not RoastPhase.OPERATOR_RECOVERY_REQUIRED:
                self.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
            self._events.emit(RoastEventKind.RECOVERY_REQUIRED, evaluation.model_dump(mode="json"))
            return True
        # CLAMP/REJECT never arise from telemetry-stage rules.
        return False

    async def _run_advisory(self, telemetry: RoastTelemetry | None) -> None:
        """Advisory step: timeout-bounded, never blocks the tick.

        Failure of any kind becomes a REJECT evaluation with the
        deterministic hold-current-targets fallback (E3-S3).
        """
        self._advisory_requested = False
        if self._advisor is None or telemetry is None or self._profile is None:
            return
        context = self._build_advisor_context(telemetry)
        try:
            decision = await asyncio.wait_for(
                self._advisor.get_recommendation(context),
                timeout=self._config.advisory_timeout_seconds,
            )
        except TimeoutError:
            await self._record_advisor_failure("timeout")
            return
        except Exception:
            await self._record_advisor_failure("provider_error")
            return
        evaluation = self._safety.evaluate_command(
            requested_heat=decision.target_heat,
            requested_fan=decision.target_fan,
            seconds_since_last_command=self._seconds_since_last_command(),
        )
        await self._snapshots.persist_evaluation(evaluation)
        self._events.emit(
            RoastEventKind.ADVISORY,
            {
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
            # Drop execution wires in E4-S3/S4 (drop_beans via the matrix);
            # E4-S2 records the eligibility verdict only.

    async def _record_advisor_failure(self, status: Literal["timeout", "provider_error"]) -> None:
        evaluation = self._safety.evaluate_advisor_failure(
            status=status,
            current_heat=self._current_heat,
            current_fan=self._current_fan,
        )
        await self._snapshots.persist_evaluation(evaluation)
        self._events.emit(
            RoastEventKind.ADVISORY, {"evaluation": evaluation.model_dump(mode="json")}
        )

    def load_profile(self, profile: RoastProfile) -> None:
        """Set the active roast profile.

        Interim public surface: E4-S4's run lifecycle (start/recover/end)
        absorbs this; the advisory step requires a profile for context.
        """
        self._profile = profile
        if self._run_started_monotonic is None:
            self._run_started_monotonic = self._clock()

    def _build_advisor_context(self, telemetry: RoastTelemetry) -> AdvisorContext:
        assert self._profile is not None  # guarded by caller
        elapsed = (
            0.0
            if self._run_started_monotonic is None
            else self._clock() - self._run_started_monotonic
        )
        return AdvisorContext(
            phase=self._phase,
            roast_elapsed_seconds=elapsed,
            development_elapsed_seconds=None,
            current_bean_temp_c=telemetry.bean_temp_c,
            current_env_temp_c=telemetry.env_temp_c,
            bean_ror_c_per_min=telemetry.bean_ror_c_per_min,
            env_ror_c_per_min=telemetry.env_ror_c_per_min,
            target_drop_temp_c=self._profile.target_drop_temp_c,
            profile_name=self._profile.name,
            first_crack_detected=telemetry.first_crack_detected,
        )

    def _seconds_since_last_command(self) -> float | None:
        if self._last_command_monotonic is None:
            return None
        return self._clock() - self._last_command_monotonic
