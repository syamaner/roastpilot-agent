"""Replay harness (component plan §7; E10-S1).

Streams a recorded roast export (the ``roast.jsonl`` telemetry + event
records a real roast produced) back out through the **real** SSE pipeline:
UI development without hardware, deterministic Playwright snapshots, and the
talk's 1× screen-capture rig (E12). It complements — never replaces —
full-loop MCP-mock simulation.

The design invariant: replay does **not** invent a parallel event path. It
drives the real :class:`~roastpilot_agent.api.RoastService` →
:class:`~roastpilot_agent.api.RoastRunner` →
:class:`~roastpilot_agent.controller.RoastController` by feeding the recorded
telemetry through a :class:`ReplayRoasterControl` that satisfies the
controller's ``StateReader`` / ``CommandExecutor`` protocols, exactly as the
E9 vertical slice drives a fake roaster. So every frame the browser receives
is a standard typed :class:`~roastpilot_agent.models.SseEvent`, the agent
phase is genuinely server-derived (the controller's own state machine, never
inferred from the export), and ``GET /api/roasts/{id}`` / ``/timeline`` /
``/telemetry`` are populated from the same store writes a live roast makes.
Temperatures are Celsius throughout — the source exports already are.

Two things in the export do not exist on the wire a live roast produces, and
are **synthesized** here, clearly labelled:

- **The advisory CLAMP key frame.** The recorded exports carry no advisory or
  safety-verdict records, and a genuine CLAMP can only arise from an
  out-of-bounds heat/fan request — which ``advisor.RoastDecision``'s 0–100
  field bounds structurally prevent (see ``safety.evaluate_command``). The SPA
  renders CLAMP from *recorded trace* (the advisory panel and the detail trace
  table read historical payloads; they do not re-run safety), so the overlay
  emits one deterministic ``advisory`` event carrying a CLAMP verdict. The
  verdict record itself is **computed by the real**
  ``SafetyPolicy.evaluate_command`` (with an injected 105 % request), so the
  reason string and clamped values are policy-accurate rather than a literal
  that could drift — but the 105 % request is injected by this overlay, not
  produced by a live advisor. The payload carries ``synthesized: true`` /
  ``source: "replay_overlay"`` so no reader mistakes it for live output.

- **The pre-T0 thermal-overrun fault fixture.** The real 7-Jun roasts never
  fault (their pre-T0 bean temperature stays under the 200 °C bound), so the
  ``fault-pre-t0`` fixture is a short hand-authored telemetry track that drives
  bean temperature past the bound. The fault + ``recovery_required`` it
  produces are emitted by the **real** ``SafetyPolicy`` through the real
  controller — only the telemetry track is synthetic.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from roastpilot_agent.api import QueuedOperatorAction, RoastService, create_app
from roastpilot_agent.config import AppConfig
from roastpilot_agent.models import (
    OperatorAction,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyPolicy
from roastpilot_agent.store import RoastStore

#: The replay speed bounds (kickoff §4): 1× is the E12 screen-recording rig,
#: 60× the fast development pass. A requested speed is clamped into this band.
MIN_SPEED = 1.0
MAX_SPEED = 60.0

#: The injected, out-of-bounds heat request used to compute the synthesized
#: CLAMP key frame. 105 % clamps to 100 % through the real safety policy.
_CLAMP_REQUEST_HEAT = 105
_CLAMP_REQUEST_FAN = 40


def clamp_speed(speed: float) -> float:
    """Clamp a requested replay speed into the supported 1×–60× band."""
    return max(MIN_SPEED, min(MAX_SPEED, speed))


class ReplayMarker(Enum):
    """Named milestones a Playwright/dev caller can deterministically run to.

    These are the SPA baseline states (kickoff §5): ``preheating`` is the
    charge-band-visible pre-T0 dashboard; ``clamp`` is the talk's advisory key
    frame; ``fault`` / ``recovery`` come from the synthetic overrun fixture.
    ``end`` is the recorded run's terminal frame. A plain ``Enum`` (D15) — the
    HTTP control surface validates the wire string against it.
    """

    PREHEATING = "preheating"
    T0 = "t0"
    FIRST_CRACK = "first_crack"
    CLAMP = "clamp"
    DROP = "drop"
    COOLING = "cooling"
    RECOVERY = "recovery"
    FAULT = "fault"
    END = "end"


class ReplayActionKind(Enum):
    """An operator action the schedule injects at a recorded marker.

    The recorded MCP export records ``beans_dropped`` / ``cooling_started`` /
    ``cooling_stopped`` as facts; replay reproduces them by submitting the
    matching operator action through the real queue + safety policy at the
    tick the marker lands on (never a direct controller poke)."""

    DROP_BEANS = "drop_beans"
    STOP_COOLING = "stop_cooling"


@dataclass(frozen=True)
class ReplayFrame:
    """One recorded telemetry sample projected to the controller's reading.

    ``telemetry`` is what ``read_telemetry`` yields for this tick; ``markers``
    are the named milestones that fire on it (e.g. ``t0`` once
    ``t0_detected`` flips on); ``inject`` is an operator action to submit
    before this tick's controller step (a recorded drop/cooling event)."""

    index: int
    telemetry: RoastTelemetry
    monotonic_seconds: float
    markers: tuple[ReplayMarker, ...] = ()
    inject: ReplayActionKind | None = None


@dataclass
class ReplayScript:
    """A parsed replay export: ordered frames + the fixture's profile.

    Built by :func:`load_export`. ``clamp_after_marker`` is the marker the
    synthesized CLAMP key frame is emitted right after (``first_crack`` for the
    demo roast, so it lands on the development-phase advisory panel)."""

    frames: list[ReplayFrame]
    profile: RoastProfile
    clamp_after_marker: ReplayMarker | None = field(default=ReplayMarker.FIRST_CRACK)


# --- Fixture parsing -------------------------------------------------------


def _profile_for(name: str) -> RoastProfile:
    """A minimal static profile for the replayed run (D7: no curve targets).

    The recorded exports carry no agent profile, so replay supplies a generic
    one. The charge guidance band (170–200 °C) is the default; the dashboard's
    charge band renders from it during preheating."""
    return RoastProfile(
        name=name,
        bean_origin="Replay (recorded roast)",
        bean_weight_grams=250.0,
        initial_heat_percent=80,
        initial_fan_percent=10,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


def _telemetry_from_record(
    record: dict[str, Any], *, t0: bool, first_crack: bool
) -> RoastTelemetry:
    """Project one recorded telemetry record into a controller ``RoastTelemetry``.

    Detection booleans are *latched* by the caller (once T0/FC is reached in
    the recording it stays true), mirroring the real MCP status fields rather
    than the raw per-frame export, which carries no detection flags."""
    return RoastTelemetry(
        bean_temp_c=float(record["bean_temp_c"]),
        env_temp_c=float(record["env_temp_c"]),
        bean_ror_c_per_min=record.get("bean_ror_c_per_min"),
        env_ror_c_per_min=record.get("env_ror_c_per_min"),
        t0_detected=t0,
        first_crack_detected=first_crack,
        cooling_on=bool(record.get("cooling_on", False)),
    )


def load_export(export_dir: Path) -> ReplayScript:
    """Parse a ``tests/fixtures/replay/<name>/`` export into a replay script.

    Reads ``roast.jsonl`` (telemetry + event records). The event records
    (``beans_added`` / ``first_crack_detected`` / ``beans_dropped`` /
    ``cooling_started`` / ``cooling_stopped``) latch the detection booleans and
    tag the nearest telemetry frame with the marker + any operator action to
    inject — so the recorded MCP facts replay through the real controller and
    operator queue, not a parallel path.
    """
    jsonl = export_dir / "roast.jsonl"
    if not jsonl.is_file():
        raise FileNotFoundError(f"replay export missing roast.jsonl: {jsonl}")

    telemetry_records: list[dict[str, Any]] = []
    event_records: list[dict[str, Any]] = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if record.get("type") == "event":
            event_records.append(record)
        else:
            telemetry_records.append(record)
    if not telemetry_records:
        raise ValueError(f"replay export has no telemetry records: {jsonl}")

    # Monotonic offsets at which each recorded MCP milestone fired.
    t0_at = _event_monotonic(event_records, "beans_added")
    fc_at = _event_monotonic(event_records, "first_crack_detected")
    drop_at = _event_monotonic(event_records, "beans_dropped")
    cool_stop_at = _event_monotonic(event_records, "cooling_stopped")

    frames: list[ReplayFrame] = []
    seen: set[ReplayMarker] = set()
    drop_injected = False
    stop_injected = False
    for index, record in enumerate(telemetry_records):
        mono = float(record["monotonic_seconds"])
        t0 = t0_at is not None and mono >= t0_at
        first_crack = fc_at is not None and mono >= fc_at
        telemetry = _telemetry_from_record(record, t0=t0, first_crack=first_crack)

        markers: list[ReplayMarker] = []
        if ReplayMarker.PREHEATING not in seen:
            markers.append(ReplayMarker.PREHEATING)
            seen.add(ReplayMarker.PREHEATING)
        if t0 and ReplayMarker.T0 not in seen:
            markers.append(ReplayMarker.T0)
            seen.add(ReplayMarker.T0)
        if first_crack and ReplayMarker.FIRST_CRACK not in seen:
            markers.append(ReplayMarker.FIRST_CRACK)
            seen.add(ReplayMarker.FIRST_CRACK)

        inject: ReplayActionKind | None = None
        if drop_at is not None and mono >= drop_at and not drop_injected:
            inject = ReplayActionKind.DROP_BEANS
            drop_injected = True
            markers.append(ReplayMarker.DROP)
            markers.append(ReplayMarker.COOLING)  # drop engages cooling
            seen.update({ReplayMarker.DROP, ReplayMarker.COOLING})
        elif cool_stop_at is not None and mono >= cool_stop_at and not stop_injected:
            inject = ReplayActionKind.STOP_COOLING
            stop_injected = True

        frames.append(
            ReplayFrame(
                index=index,
                telemetry=telemetry,
                monotonic_seconds=mono,
                markers=tuple(markers),
                inject=inject,
            )
        )

    # The last frame ends the run.
    frames[-1] = _with_marker(frames[-1], ReplayMarker.END)
    return ReplayScript(frames=frames, profile=_profile_for(export_dir.name))


def _event_monotonic(events: list[dict[str, Any]], kind: str) -> float | None:
    """The monotonic offset of the first recorded event of ``kind``, if any."""
    for event in events:
        if event.get("kind") == kind:
            return float(event["monotonic_seconds"])
    return None


def _with_marker(frame: ReplayFrame, marker: ReplayMarker) -> ReplayFrame:
    """Return ``frame`` with ``marker`` appended (idempotent)."""
    if marker in frame.markers:
        return frame
    return ReplayFrame(
        index=frame.index,
        telemetry=frame.telemetry,
        monotonic_seconds=frame.monotonic_seconds,
        markers=(*frame.markers, marker),
        inject=frame.inject,
    )


# --- Replay roaster control ------------------------------------------------


class _SimClock:
    """A monotonic clock pinned to the recorded sim-time of the current frame.

    The controller derives ``roast_elapsed_seconds`` as
    ``clock() - run_started_clock`` (see ``RoastController``). On the live path
    that clock is real ``time.monotonic``; on the replay path that is wrong in
    ``--step`` mode — a stepped burst drains every frame in a few milliseconds
    of wall time, so every telemetry frame would report the same ~instant
    elapsed and the dashboard curve collapses onto one x (#128). Instead the
    replay source advances this clock to each frame's recorded
    ``monotonic_seconds`` (sim-time) before the controller reads/ticks, so
    elapsed spreads across the recorded duration for both stepped and 1× modes.
    Set/read through :class:`ReplaySource`; ``time.monotonic`` is never consulted
    on the replay tick path.
    """

    def __init__(self) -> None:
        #: The current frame's recorded sim-time, in seconds. Set by the source
        #: before each controller interaction; read by the controller/runner.
        self.now: float = 0.0

    def __call__(self) -> float:
        """Return the recorded sim-time of the frame currently being processed."""
        return self.now


class ReplayRoasterControl:
    """A ``StateReader`` + ``CommandExecutor`` backed by recorded frames.

    ``read_telemetry`` yields the next recorded frame's reading (the last frame
    repeats once exhausted, so a terminal phase keeps a stable reading). Writes
    are recorded no-ops: the recorded roast already happened, so replay does not
    actuate anything — but the controller still owns and safety-evaluates every
    write exactly as live, so the decision trace is faithful. ``last_state``
    is ``None`` (the export carries no full ``RoastSessionState``); the runner
    tolerates that and simply persists no raw-state enrichment.
    """

    def __init__(self) -> None:
        self._frames: list[RoastTelemetry] = []
        self._cursor = 0
        self.commands: list[tuple[str, dict[str, object]]] = []

    def load(self, frames: list[RoastTelemetry]) -> None:
        """Install the ordered telemetry frames the reader will yield."""
        self._frames = frames
        self._cursor = 0

    def advance(self) -> None:
        """Step the read cursor to the next frame (clamped at the last)."""
        if self._cursor < len(self._frames) - 1:
            self._cursor += 1

    @property
    def last_state(self) -> None:
        """No raw ``RoastSessionState`` from a flat export (runner tolerates)."""
        return None

    async def read_telemetry(self) -> RoastTelemetry | None:
        if not self._frames:
            return None
        return self._frames[self._cursor]

    async def start_session(self) -> None:
        self.commands.append(("start_session", {}))

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self.commands.append(("set_targets", {"heat": heat_percent, "fan": fan_percent}))

    async def mark_beans_added(self) -> None:
        self.commands.append(("mark_beans_added", {}))

    async def mark_first_crack(self) -> None:
        self.commands.append(("mark_first_crack", {}))

    async def drop_beans(self) -> None:
        self.commands.append(("drop_beans", {}))

    async def start_cooling(self) -> None:
        self.commands.append(("start_cooling", {}))

    async def stop_cooling(self) -> None:
        self.commands.append(("stop_cooling", {}))

    async def emergency_stop(self, *, reason: str) -> None:
        self.commands.append(("emergency_stop", {"reason": reason}))


# --- Replay step result ----------------------------------------------------


@dataclass(frozen=True)
class ReplayStepResult:
    """The settled state after a deterministic step/advance (HTTP body shape).

    ``last_event_id`` is the broadcaster's sequence after the stepped ticks
    drain — the **same** id the SSE frames carry — so a Playwright caller can
    wait until the browser's ``lastEventId >= last_event_id`` before
    screenshotting, with no arbitrary sleep. ``settled`` is always true on
    return (the step ran synchronously to completion).

    ``requested_marker`` / ``marker_reached`` are populated only by
    :meth:`ReplaySource.advance_to`: ``marker_reached`` is ``False`` when the
    marker never fired (the export exhausted first), which the control route
    turns into a 404 so a Playwright caller fails loud on a wrong fixture rather
    than screenshotting the wrong state. ``step`` leaves them ``None`` / ``True``
    (count-based, no marker to miss)."""

    agent_phase: str
    tick: int
    elapsed_seconds: float | None
    finalized: bool
    settled: bool
    last_event_id: int
    requested_marker: str | None = None
    marker_reached: bool = True

    def to_json(self) -> dict[str, Any]:
        """The JSON body the ``/api/replay`` control routes return."""
        return {
            "agent_phase": self.agent_phase,
            "tick": self.tick,
            "elapsed_seconds": self.elapsed_seconds,
            "finalized": self.finalized,
            "settled": self.settled,
            "last_event_id": self.last_event_id,
            "requested_marker": self.requested_marker,
            "marker_reached": self.marker_reached,
        }


# --- Replay source ---------------------------------------------------------


class ReplaySource:
    """Drives a recorded export through the real SSE pipeline (E10-S1).

    Two modes share one deterministic stepping core:

    - **Free-running** (``run``): advance every frame, sleeping
      ``tick_interval / speed`` between ticks. 1× is the E12 screen-recording
      rig; up to 60× for development. Cancellable.
    - **Stepped** (``step`` / ``advance_to``): no wall clock at all — the HTTP
      ``--step`` control surface calls these so Playwright lands on an exact
      frame/marker. A frame appears on the SSE stream exactly when stepped to.

    The source owns the :class:`ReplayRoasterControl` and submits the recorded
    operator actions (drop/stop-cooling) through the service's real operator
    queue at their recorded frames, so they pass the full safety policy.
    """

    def __init__(
        self,
        export_dir: Path,
        service: RoastService,
        *,
        speed: float = 1.0,
        control: ReplayRoasterControl,
        safety: SafetyPolicy,
        store: RoastStore,
        sim_clock: _SimClock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_interval_seconds: float = 1.0,
    ) -> None:
        self._script = load_export(export_dir)
        self._service = service
        self._speed = clamp_speed(speed)
        self._control = control
        self._safety = safety
        self._store = store
        #: The controller's clock on the replay path. Advanced to each frame's
        #: recorded sim-time before the controller reads/ticks, so elapsed
        #: tracks the recording, not wall time (#128).
        self._sim_clock = sim_clock
        self._sleep = sleep
        self._tick_interval = tick_interval_seconds
        self._run_id: str | None = None
        self._cursor = 0  # the next frame index to advance into
        self._reached: set[ReplayMarker] = set()
        self._clamp_emitted = False
        self._started = False

    @property
    def run_id(self) -> str | None:
        """The replayed run's id (``None`` until :meth:`start`)."""
        return self._run_id

    def set_speed(self, speed: float) -> None:
        """Set the free-running replay speed, clamped to the 1×–60× band."""
        self._speed = clamp_speed(speed)

    async def aclose(self) -> None:
        """Stop the live loop and close the store (test/teardown helper).

        The CLI serves under the app lifespan (which closes both on shutdown);
        a direct ``create_replay_app`` caller that never enters the lifespan —
        e.g. a unit test stepping the source — calls this so no aiosqlite worker
        thread outlives the event loop."""
        await self._service.shutdown()
        await self._store.close()

    @property
    def frame_count(self) -> int:
        """Total recorded frames in the loaded export."""
        return len(self._script.frames)

    @property
    def issued_commands(self) -> list[str]:
        """The roaster-control command names issued so far (test/debug view).

        Replay actuates nothing, but the controller still routes every write
        through the control surface, so this is the executed-command trail —
        e.g. asserting STOP_COOLING actually fired."""
        return [name for name, _ in self._control.commands]

    async def start(self) -> str:
        """Create the run and drive the controller's idle→preheating start.

        Installs the first frame as the reader's reading, starts the roast
        through the real service (which runs ``start_run`` → preheating and
        emits ``run_started`` + the first ``telemetry``), and leaves the source
        paused at tick 0 ready to step. Returns the run id.
        """
        if self._started:  # pragma: no cover — guarded by callers
            raise RuntimeError("replay source already started")
        self._control.load([frame.telemetry for frame in self._script.frames])
        # Pin the controller's clock to frame 0's recorded sim-time so the run's
        # elapsed baseline (run_started) is captured in sim-time, not wall time.
        self._sim_clock.now = self._script.frames[0].monotonic_seconds
        detail = await self._service.start_roast(self._script.profile)
        self._run_id = detail.id
        self._started = True
        # The first frame's start-up markers (preheating) are reached at boot.
        self._mark(self._script.frames[0])
        return detail.id

    async def step(self, ticks: int = 1) -> ReplayStepResult:
        """Advance exactly ``ticks`` recorded frames through the controller.

        Synchronous and wall-clock-free — each step advances the reader cursor,
        injects any recorded operator action for that frame, runs one real
        controller tick, and (after the configured marker) emits the synthesized
        CLAMP key frame once. Returns the settled state."""
        finalized = False
        for _ in range(max(0, ticks)):
            finalized = await self._advance_one()
            if finalized:
                break
        return self._result(finalized)

    async def advance_to(self, marker: ReplayMarker) -> ReplayStepResult:
        """Advance until ``marker`` fires (or the run finalizes / frames exhaust).

        Robust to fixture edits (markers, not tick numbers). If the marker was
        already reached, returns the current settled state without stepping. The
        result's ``marker_reached`` is ``False`` when the export exhausted before
        the marker fired (e.g. ``fault`` against a roast that never faults) — the
        control route turns that into a 404 so a caller fails loud rather than
        screenshotting the wrong state."""
        if marker in self._reached:
            return self._result(self._is_finalized(), marker=marker)
        finalized = False
        while marker not in self._reached and not finalized:
            if self._cursor >= len(self._script.frames):
                break
            finalized = await self._advance_one()
        return self._result(finalized, marker=marker)

    async def run(self) -> None:
        """Free-running replay: advance every frame at ``tick_interval / speed``.

        The screen-recording / dev path. Cancellable; stops at the terminal
        frame. Steps share the same core as :meth:`step`, so the event stream is
        identical to the stepped path — only the inter-tick delay differs."""
        delay = self._tick_interval / self._speed
        while self._cursor < len(self._script.frames):
            if await self._advance_one():
                break
            await self._sleep(delay)

    # --- stepping core -----------------------------------------------------

    async def _advance_one(self) -> bool:
        """Advance one frame: inject recorded action → tick → CLAMP overlay.

        Returns whether the run is now finalized. Shared by every mode so the
        stepped and free-running paths produce byte-identical event streams."""
        if self._cursor >= len(self._script.frames):
            return self._is_finalized()
        frame = self._script.frames[self._cursor]
        # Advance the controller's clock to this frame's recorded sim-time before
        # it reads/ticks, so roast_elapsed_seconds tracks the recording rather
        # than wall time — the fix for stepped-burst x-axis collapse (#128).
        self._sim_clock.now = frame.monotonic_seconds
        # Move the reader onto this frame's reading before the controller reads.
        if self._cursor > 0:
            self._control.advance()
        if frame.inject is not None:
            await self._inject(frame.inject)
        runner = self._service.runner
        finalized = False
        if runner is not None:
            finalized = await runner.tick_once()
        self._mark(frame)
        self._mark_from_phase()
        await self._maybe_emit_clamp(frame)
        self._cursor += 1
        return finalized

    def _mark_from_phase(self) -> None:
        """Derive phase-based markers from the controller's post-tick phase.

        The ``fault`` / ``recovery`` milestones have no export event record (the
        synthetic overrun fixture never reaches a recorded event) — they are the
        controller's *resulting* phase after the real safety policy fires, so
        they are read from the controller, never pre-parsed."""
        runner = self._service.runner
        if runner is None:  # pragma: no cover — runner wired before stepping
            return
        phase = runner.controller_snapshot().phase
        if phase is RoastPhase.FAULTED:
            self._reached.add(ReplayMarker.FAULT)
        elif phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED:
            self._reached.add(ReplayMarker.RECOVERY)

    async def _inject(self, action: ReplayActionKind) -> None:
        """Submit a recorded operator action through the real operator queue.

        Drop/stop-cooling replay as genuine operator actions so they pass the
        full command×phase safety policy on drain — never a direct controller
        poke. ``run_id`` is set (start ran first)."""
        run_id = self._run_id
        if run_id is None:  # pragma: no cover — start() always precedes stepping
            return
        mapping = {
            ReplayActionKind.DROP_BEANS: OperatorAction.DROP_BEANS,
            ReplayActionKind.STOP_COOLING: OperatorAction.STOP_COOLING,
        }
        self._service.operator_queue.put_nowait(
            QueuedOperatorAction(run_id=run_id, action=mapping[action], payload=None)
        )

    def _mark(self, frame: ReplayFrame) -> None:
        """Record the markers this frame reached."""
        self._reached.update(frame.markers)

    async def _maybe_emit_clamp(self, frame: ReplayFrame) -> None:
        """Emit the synthesized CLAMP key frame once, after its trigger marker.

        The CLAMP is **synthesized demo trace** — the recorded export has no
        advisory records, and a genuine CLAMP cannot arise from a bounded
        ``RoastDecision`` (see the module docstring). The verdict record is
        computed by the **real** ``SafetyPolicy.evaluate_command`` with an
        injected 105 % heat request, so its reason + clamped values are
        policy-accurate; only the request is injected. The payload is tagged so
        no reader mistakes it for live-evaluated output. It is both emitted to
        SSE (the live advisory panel) and persisted as a ``roast_events`` +
        ``safety_evaluations`` row (so ``/timeline`` — the detail trace table —
        shows it); the SPA renders it from that recorded trace, never re-running
        safety."""
        trigger = self._script.clamp_after_marker
        run_id = self._run_id
        if self._clamp_emitted or trigger is None or trigger not in self._reached:
            return
        if run_id is None:  # pragma: no cover — start() always precedes stepping
            return
        evaluation = self._safety.evaluate_command(
            requested_heat=_CLAMP_REQUEST_HEAT,
            requested_fan=_CLAMP_REQUEST_FAN,
            seconds_since_last_command=None,
        )
        decision = {
            "target_heat": _CLAMP_REQUEST_HEAT,
            "target_fan": _CLAMP_REQUEST_FAN,
            "should_drop": False,
            "confidence": 0.71,
            "rationale": (
                "Bean RoR is crashing into the development phase; request more heat "
                "to hold momentum."
            ),
        }
        payload: dict[str, Any] = {
            "trigger": "replay_overlay",
            "synthesized": True,
            "source": "replay_overlay",
            "decision": decision,
            "evaluation": evaluation.model_dump(mode="json"),
        }
        # SSE (live advisory panel) + store (detail-page trace table).
        self._service.events.emit(RoastEventKind.ADVISORY, payload)
        tick = self._current_tick()
        await self._store.record_safety_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)
        await self._store.record_event(
            run_id=run_id,
            kind=RoastEventKind.ADVISORY,
            source=RoastEventSource.ADVISOR,
            payload=payload,
        )
        self._clamp_emitted = True
        self._reached.add(ReplayMarker.CLAMP)

    def _current_tick(self) -> int:
        runner = self._service.runner
        return 0 if runner is None else runner.current_tick

    def _is_finalized(self) -> bool:
        runner = self._service.runner
        return runner is not None and runner.finalized

    def _result(self, finalized: bool, *, marker: ReplayMarker | None = None) -> ReplayStepResult:
        phase, elapsed, tick = self._snapshot_fields()
        return ReplayStepResult(
            agent_phase=phase,
            tick=tick,
            elapsed_seconds=elapsed,
            finalized=finalized or self._is_finalized(),
            settled=True,
            last_event_id=self._service.events.last_event_id,
            requested_marker=None if marker is None else marker.value,
            marker_reached=marker is None or marker in self._reached,
        )

    def _snapshot_fields(self) -> tuple[str, float | None, int]:
        """Read phase/elapsed/tick from the live controller snapshot."""
        runner = self._service.runner
        if runner is None:  # pragma: no cover — runner is wired before stepping
            return ("idle", None, self._cursor)
        snap = runner.controller_snapshot()
        return (snap.phase.value, snap.roast_elapsed_seconds, self._cursor)


def build_replay_service(
    export_dir: Path,
    store_path: Path,
    *,
    config: AppConfig | None = None,
) -> tuple[RoastService, ReplaySource, RoastStore]:
    """Wire a real :class:`RoastService` + :class:`ReplaySource` for an export.

    The service is fully real (store, broadcaster, operator queue, controller
    loop disabled — ``run_loop=False`` so the source owns stepping). Returns the
    store too so the caller owns its lifecycle (``initialize`` before serving,
    ``close`` after). Shared by the CLI (``--replay``) and the tests.
    """
    app_config = config or AppConfig()
    control = ReplayRoasterControl()
    safety = SafetyPolicy(app_config.safety)
    store = RoastStore(store_path)
    # One sim clock, shared: the source advances it to each frame's recorded
    # sim-time and the service threads it into the controller + runner, so
    # elapsed tracks the recording, not wall time (#128).
    sim_clock = _SimClock()
    service = RoastService(
        store,
        config=app_config,
        roaster=control,
        advisor=None,
        exporter=None,
        raw_state=control,
        run_loop=False,
        clock=sim_clock,
    )
    source = ReplaySource(
        export_dir,
        service,
        control=control,
        safety=safety,
        store=store,
        sim_clock=sim_clock,
        tick_interval_seconds=app_config.controller.tick_interval_seconds,
    )
    return service, source, store


# --- HTTP step-control surface (mounted ONLY in --step replay mode) ---------
#
# These routes advance the replay deterministically over HTTP so a Playwright
# `@playwright/test` global-setup (Node, HTTP-only) can land the SPA on an exact
# frame/marker. They are a control hole on a live roast (they drive the tick
# loop), so they are mounted ONLY by `mount_replay_controls` on the dedicated
# `--step` app — never by `api.create_app`. A test asserts the live app never
# exposes them.


class ReplayStepRequest(BaseModel):
    """``POST /api/replay/step`` body: advance N recorded frames."""

    ticks: int = 1


class ReplayAdvanceRequest(BaseModel):
    """``POST /api/replay/advance-to`` body: run until a named marker fires."""

    marker: ReplayMarker


def mount_replay_controls(app: FastAPI, source: ReplaySource) -> None:
    """Mount the gated ``/api/replay/{step,advance-to}`` control routes.

    Only the ``--step`` replay app calls this. Each route advances the real
    controller synchronously and returns the settled :class:`ReplayStepResult`
    (phase, tick, ``last_event_id`` for the deterministic wait). ``advance-to``
    returns **404** when the requested marker never fires in the export, so a
    Playwright caller fails loud on a wrong fixture/marker instead of
    screenshotting the wrong (terminal) state."""

    async def step(request: ReplayStepRequest) -> dict[str, Any]:
        return (await source.step(request.ticks)).to_json()

    async def advance_to(request: ReplayAdvanceRequest) -> dict[str, Any]:
        result = await source.advance_to(request.marker)
        if not result.marker_reached:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"marker {request.marker.value!r} never fired in this "
                    f"{source.frame_count}-frame export; reached end at tick {result.tick} "
                    f"(phase {result.agent_phase})"
                ),
            )
        return result.to_json()

    app.post("/api/replay/step")(step)
    app.post("/api/replay/advance-to")(advance_to)


async def create_replay_app(
    export_dir: Path,
    store_path: Path,
    *,
    config: AppConfig | None = None,
    step_mode: bool = False,
    speed: float = 1.0,
) -> tuple[FastAPI, RoastService, ReplaySource]:
    """Build a fully-wired replay FastAPI app over a recorded export.

    Initializes the store, starts the replayed run (idle→preheating), and
    returns the app + service + source. In ``step_mode`` the gated control
    routes are mounted and the source is left paused at tick 0; otherwise the
    caller free-runs :meth:`ReplaySource.run` at ``speed`` (1× = recording rig).
    The live ``api.create_app`` is reused unchanged — replay adds only the
    gated routes and a no-recovery lifespan, never a parallel app.

    The lifespan override is load-bearing: the run is already active (replay
    drives it), so the live app's restart-recovery startup would wrongly force
    it into ``operator_recovery_required``. Replay owns the run lifecycle, so
    its lifespan only stops the live loop on shutdown.
    """
    service, source, store = build_replay_service(export_dir, store_path, config=config)
    source.set_speed(speed)
    await store.initialize()
    await source.start()

    @contextlib.asynccontextmanager
    async def _replay_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # No recover_on_start — replay owns the (already-active) run; on shutdown
        # tear down the live loop (mirroring the live lifespan) and close the
        # store this factory owns, so no aiosqlite thread outlives the loop.
        yield
        await service.shutdown()
        await store.close()

    app = create_app(service, lifespan=_replay_lifespan)
    if step_mode:
        mount_replay_controls(app, source)
    return app, service, source
