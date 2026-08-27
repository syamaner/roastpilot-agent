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

Three things in the export do not exist on the wire a live roast produces,
and are **synthesized** here, clearly labelled:

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

- **The ambient temp/humidity/pressure triad.** The recorded exports predate
  the #342 ambient probe and the live agent only mirrors the MCP's *current*
  ambient reading each tick (no per-tick history is persisted), so there is no
  real ambient value in an export to read back. ``_telemetry_from_record``
  sets a fixed, representative ``(21.0 °C, 45 %, 1013 hPa)`` reading on every
  frame — see :func:`_synthesized_ambient` — so the dashboard's "Room" readout
  renders a populated state in replay/demo mode rather than the honest-but-
  empty em-dash a ``None`` would force.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from roastpilot_agent.api import QueuedOperatorAction, RoastService, create_app
from roastpilot_agent.config import (
    AppConfig,
    JointWindowPlanner,
    PostFirstCrackControl,
    ReferenceCurve,
)
from roastpilot_agent.live import mount_spa
from roastpilot_agent.mcp_client import (
    EventSnapshot,
    MalformedCommandResultError,
    applied_state_from_event,
)
from roastpilot_agent.models import (
    AppliedRoasterState,
    MicStatus,
    OperatorAction,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyPolicy
from roastpilot_agent.store import RoastStore

_log = logging.getLogger(__name__)

#: The replay speed bounds (kickoff §4): 1× is the E12 screen-recording rig,
#: 60× the fast development pass. A requested speed is clamped into this band.
MIN_SPEED = 1.0
MAX_SPEED = 60.0

#: The real drivers' documented ``drop_beans()`` side effect (heat 0 %, fan
#: 100 %, cooling on — coffee_roaster_mcp.drivers), used as the fallback when
#: an export's ``beans_dropped`` event carries no parseable applied-state
#: payload (an older export predating #507, or a hand-authored fixture with
#: no event records at all, e.g. ``fault-pre-t0``). Every currently-committed
#: export with a recorded drop DOES carry this exact payload (#507
#: safety-review LOW-1) — this is a fallback for fixture staleness, not the
#: primary source.
_FALLBACK_DROP_APPLIED_STATE = AppliedRoasterState(
    heat_level_percent=0, fan_level_percent=100, cooling_on=True
)

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
    demo roast, so it lands on the development-phase advisory panel).

    ``drop_applied_state`` is the applied heat/fan/cooling read off the
    export's own recorded ``beans_dropped`` event payload (#507 safety-review
    LOW-1) — replay-reproduces-history: :class:`ReplayRoasterControl` returns
    THIS from ``drop_beans()`` rather than a hardcoded driver constant, so a
    future export recorded against different driver constants replays
    faithfully instead of silently diverging. Falls back to
    :data:`_FALLBACK_DROP_APPLIED_STATE` when the export carries no
    ``beans_dropped`` event or a malformed payload (an export predating #507,
    or a hand-authored fixture with no event records at all)."""

    frames: list[ReplayFrame]
    profile: RoastProfile
    clamp_after_marker: ReplayMarker | None = field(default=ReplayMarker.FIRST_CRACK)
    drop_applied_state: AppliedRoasterState = field(
        default_factory=lambda: _FALLBACK_DROP_APPLIED_STATE
    )


# --- Fixture parsing -------------------------------------------------------


def _profile_for(name: str) -> RoastProfile:
    """A minimal static profile for the replayed run (D7: no curve targets).

    The recorded exports carry no agent profile, so replay supplies a generic
    one. The charge guidance band (170–200 °C) is the default; the dashboard's
    charge band renders from it during preheating.

    The initial heat/fan match the deterministic pre-FC levers (D35/#222: heat
    100 / fan low) so the run-start command sits inside the narrowed PREHEATING
    box (carry-forward A) and replays as an ALLOW, not a CLAMP — the synthesized
    CLAMP overlay stays the only CLAMP in a replayed timeline."""
    return RoastProfile(
        name=name,
        bean_origin="Replay (recorded roast)",
        bean_weight_grams=250.0,
        initial_heat_percent=100,
        initial_fan_percent=10,
        # Default target aligned with prompt v4 + the operator's empirical median
        # across the 28 good roasts (drop 195 °C / 15 % DTR): 205 °C anchored v4
        # above the ≤196 °C bitter ceiling, with no deterministic 196 ceiling in
        # safety to catch it (advisory-only; safety owns only the 230 hard max).
        # #199 (Codex #196-#1).
        target_drop_temp_c=195.0,
        target_development_percent=15.0,
    )


def _synthesized_mic_status(*, first_crack: bool) -> MicStatus:
    """A faithful audio-capture mic status for a recorded roast (#197).

    The flat exports carry no first-crack pipeline counters, so — like the
    latched detection booleans — replay synthesizes a plausible *capture-alive*
    status: audio is running, the detector is ``pending`` until FC latches then
    ``detected``. Window counts are left at zero (the export records none); the
    derived :class:`~roastpilot_agent.models.MicHealth` is OK either way. This
    is what lets the committed contract fixture pin the real ``MicStatus`` shape
    on the telemetry frame rather than ``null``."""
    return MicStatus.from_first_crack_status(
        status="detected" if first_crack else "pending",
        audio_running=True,
        queued_window_count=0,
        emitted_window_count=0,
        dropped_window_count=0,
        processed_window_count=0,
        reason=None,
    )


#: The synthesized ambient triad :func:`_synthesized_ambient` returns —
#: a plausible indoor roastery reading (Celsius; #467). Fixed module-level
#: constants so the value is documented once and trivially greppable.
_SYNTHESIZED_AMBIENT_TEMP_C = 21.0
_SYNTHESIZED_AMBIENT_HUMIDITY_PCT = 45.0
_SYNTHESIZED_AMBIENT_PRESSURE_HPA = 1013.0


def _synthesized_ambient() -> tuple[float, float, float]:
    """A plausible ambient (temp °C, humidity %, pressure hPa) triad for replay (#467).

    Recorded exports predate the #342 Yoctopuce ambient probe, and the live
    agent only mirrors the MCP's *current* ambient reading each tick — it never
    persists a per-tick ambient history — so there is no real ambient value
    anywhere in a recorded export to read back. Like :func:`_synthesized_mic_status`,
    replay synthesizes a fixed, representative reading instead: a believable
    indoor roastery condition (21.0 °C / 45 % RH / 1013 hPa), constant across
    every frame. This is what lets the "Room" readout render real values in
    replay/demo mode (and the ``dashboard-live`` Playwright baseline show a
    populated state) instead of the honest-but-unpopulated em-dash a `None`
    would otherwise force — it is not a claim that a live sensor was read.
    """
    return (
        _SYNTHESIZED_AMBIENT_TEMP_C,
        _SYNTHESIZED_AMBIENT_HUMIDITY_PCT,
        _SYNTHESIZED_AMBIENT_PRESSURE_HPA,
    )


def _telemetry_from_record(
    record: dict[str, Any], *, t0: bool, first_crack: bool
) -> RoastTelemetry:
    """Project one recorded telemetry record into a controller ``RoastTelemetry``.

    Detection booleans are *latched* by the caller (once T0/FC is reached in
    the recording it stays true), mirroring the real MCP status fields rather
    than the raw per-frame export, which carries no detection flags. The
    capture-alive ``mic_status`` (#197) is synthesized the same way — see
    :func:`_synthesized_mic_status`. The ambient triad (#467) is synthesized
    identically — see :func:`_synthesized_ambient` — since exports carry no
    ambient reading either."""
    ambient_temp_c, ambient_humidity_pct, ambient_pressure_hpa = _synthesized_ambient()
    return RoastTelemetry(
        bean_temp_c=float(record["bean_temp_c"]),
        env_temp_c=float(record["env_temp_c"]),
        bean_ror_c_per_min=record.get("bean_ror_c_per_min"),
        env_ror_c_per_min=record.get("env_ror_c_per_min"),
        t0_detected=t0,
        first_crack_detected=first_crack,
        cooling_on=bool(record.get("cooling_on", False)),
        mic_status=_synthesized_mic_status(first_crack=first_crack),
        ambient_temp_c=ambient_temp_c,
        ambient_humidity_pct=ambient_humidity_pct,
        ambient_pressure_hpa=ambient_pressure_hpa,
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
        record_type = record.get("type")
        if record_type == "event":
            event_records.append(record)
        elif record_type in (None, "telemetry"):
            # A missing ``type`` is a legacy telemetry record (pre-typed export);
            # an explicit ``"telemetry"`` is the current form.
            telemetry_records.append(record)
        else:
            # Guard the open ``else`` that previously coerced *every* non-event
            # record into a telemetry frame (#103): an unrecognised body — e.g. a
            # future ``type="summary"`` — would silently become a bogus telemetry
            # frame and crash on the missing temperature keys (or worse, plot
            # garbage). Skip it with a loud warning so a new record type is a
            # visible no-op here, not a corrupt frame.
            _log.warning(
                "replay export %s: skipping unrecognised JSONL record type %r", jsonl, record_type
            )
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
            # COOLING shares the DROP frame on the assumption that drop_beans
            # engages cooling on the Hottop. Whether the real machine actually
            # couples them is an OPEN hardware-verification story (component plan
            # §3: "whether drop_beans engages cooling is to be verified"). Fine
            # for replay — both markers should be reachable at the drop on the
            # recorded roasts — but if the distinction ever matters (a separate
            # recorded cooling_started event), split COOLING onto its own frame
            # keyed off that event, the way STOP_COOLING is keyed off
            # cooling_stopped below. (#103)
            markers.append(ReplayMarker.COOLING)
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
    return ReplayScript(
        frames=frames,
        profile=_profile_for(export_dir.name),
        drop_applied_state=_drop_applied_state_from_records(event_records),
    )


def _event_monotonic(events: list[dict[str, Any]], kind: str) -> float | None:
    """The monotonic offset of the first recorded event of ``kind``, if any."""
    for event in events:
        if event.get("kind") == kind:
            return float(event["monotonic_seconds"])
    return None


def _drop_applied_state_from_records(events: list[dict[str, Any]]) -> AppliedRoasterState:
    """Read the applied heat/fan/cooling off the export's own ``beans_dropped``
    event payload (#507 safety-review LOW-1).

    Falls back to :data:`_FALLBACK_DROP_APPLIED_STATE` when the export has no
    ``beans_dropped`` event (a fixture with no recorded drop, or a
    hand-authored telemetry-only fixture like ``fault-pre-t0``) or the event's
    payload does not carry a valid applied state (an export predating #507) —
    never crashes fixture loading over a missing/older payload shape.
    """
    for record in events:
        if record.get("kind") != "beans_dropped":
            continue
        try:
            event = EventSnapshot.model_validate(record)
        except Exception:  # noqa: BLE001 - a malformed record falls back, never crashes.
            return _FALLBACK_DROP_APPLIED_STATE
        try:
            return applied_state_from_event(event)
        except MalformedCommandResultError:
            return _FALLBACK_DROP_APPLIED_STATE
    return _FALLBACK_DROP_APPLIED_STATE


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
        #: The applied state ``drop_beans()`` returns (#507 safety-review
        #: LOW-1) — set by :meth:`load`, sourced from the export's own
        #: recorded ``beans_dropped`` event payload, falling back to
        #: :data:`_FALLBACK_DROP_APPLIED_STATE` for an unloaded/older export.
        self._drop_applied_state: AppliedRoasterState = _FALLBACK_DROP_APPLIED_STATE

    def load(
        self,
        frames: list[RoastTelemetry],
        *,
        drop_applied_state: AppliedRoasterState = _FALLBACK_DROP_APPLIED_STATE,
    ) -> None:
        """Install the ordered telemetry frames the reader will yield.

        Args:
            frames: The ordered telemetry readings ``read_telemetry`` yields.
            drop_applied_state: The state ``drop_beans()`` returns (#507),
                normally the export's own recorded ``beans_dropped`` payload.
        """
        self._frames = frames
        self._cursor = 0
        self._drop_applied_state = drop_applied_state

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

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        self.commands.append(
            (
                "start_session",
                {"recording_origin": recording_origin, "recording_roast_num": recording_roast_num},
            )
        )

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self.commands.append(("set_targets", {"heat": heat_percent, "fan": fan_percent}))

    async def mark_beans_added(self) -> None:
        self.commands.append(("mark_beans_added", {}))

    async def mark_first_crack(self) -> None:
        self.commands.append(("mark_first_crack", {}))

    async def drop_beans(self) -> AppliedRoasterState:
        self.commands.append(("drop_beans", {}))
        # Replay never actuates hardware — the recorded roast already
        # happened — so this returns what the EXPORT itself recorded the
        # driver as having applied (set by load(), #507 safety-review LOW-1),
        # not a hardcoded driver constant. Faithful to replay-reproduces-
        # history: a future export recorded against different driver
        # constants replays correctly instead of silently diverging.
        return self._drop_applied_state

    async def start_cooling(self) -> None:
        self.commands.append(("start_cooling", {}))

    async def stop_cooling(self) -> None:
        self.commands.append(("stop_cooling", {}))

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState:
        self.commands.append(("emergency_stop", {"reason": reason}))
        # Unlike drop_beans, replay never actually calls this: the synthetic
        # fault fixture (fault-pre-t0) produces its FAULT transition through
        # the real SafetyPolicy's telemetry-stage evaluation, not by injecting
        # an e-stop action (there is no ReplayActionKind.EMERGENCY_STOP), and
        # no committed export carries a recorded "fault" event to read a
        # payload from. Kept as a fixed, realistic stand-in (matching the real
        # driver's documented emergency_stop() side effect) purely to satisfy
        # the CommandExecutor protocol; #507 safety-review LOW-1 only asked
        # for drop_beans, where a real recorded payload exists to read.
        return AppliedRoasterState(heat_level_percent=0, fan_level_percent=100, cooling_on=True)


# --- Replay step result ----------------------------------------------------


@dataclass(frozen=True)
class ReplayStepResult:
    """The settled state after a deterministic step/advance (HTTP body shape).

    ``run_id`` + ``persisted_point_count`` are the **lossless** settle signal
    (#338): the run id and the number of charged telemetry rows the store holds
    after this step. A Playwright caller polls ``GET /api/roasts/{run_id}/telemetry``
    (REST, store-backed, lossless) and waits for the browser's rendered curve to
    reach this count — a settle barrier that does NOT depend on every SSE frame
    arriving. The browser re-hydrates the full series from the same REST snapshot
    on (re)connect (#153), so a dropped/queued SSE frame self-heals.

    ``last_event_id`` is the broadcaster's sequence after the stepped ticks drain
    — the same id the SSE frames carry. It is retained for diagnostics, but is the
    LOSSY signal (a dropped frame leaves a browser's ``__lastEventId`` permanently
    short, #338), so it is no longer the settle barrier. ``settled`` is always true
    on return (the step ran synchronously to completion).

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
    run_id: str | None = None
    persisted_point_count: int = 0
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
            "run_id": self.run_id,
            "persisted_point_count": self.persisted_point_count,
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
        self._control.load(
            [frame.telemetry for frame in self._script.frames],
            drop_applied_state=self._script.drop_applied_state,
        )
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
        return await self._result(finalized)

    async def step_to(self, target_tick: int) -> ReplayStepResult:
        """Advance forward until the cursor reaches an ABSOLUTE ``target_tick`` (#338).

        The idempotent sibling of :meth:`step`. ``step`` is count-based and
        additive, so under Playwright ``retries`` a re-run that calls ``step(N)``
        again advances N MORE frames from wherever the failed attempt left the
        stateful (monotonic-forward) replay agent — landing the wrong phase
        (the #338 ``toBe`` mismatch). ``step_to`` instead advances only the delta
        to an absolute cursor, so a retry on an agent already at/past the target
        is a no-op and lands the SAME state every attempt. Forward-only (the
        cursor cannot rewind); a target at/below the current cursor steps nothing.

        Args:
            target_tick: The absolute cursor index to advance the replay to.

        Returns:
            The settled state once the cursor reaches ``target_tick`` (or the run
            finalizes / the frames exhaust first).
        """
        finalized = False
        while self._cursor < target_tick and not finalized:
            if self._cursor >= len(self._script.frames):
                break
            finalized = await self._advance_one()
        return await self._result(finalized)

    async def advance_to(self, marker: ReplayMarker) -> ReplayStepResult:
        """Advance until ``marker`` fires (or the run finalizes / frames exhaust).

        Robust to fixture edits (markers, not tick numbers). If the marker was
        already reached, returns the current settled state without stepping. The
        result's ``marker_reached`` is ``False`` when the export exhausted before
        the marker fired (e.g. ``fault`` against a roast that never faults) — the
        control route turns that into a 404 so a caller fails loud rather than
        screenshotting the wrong state."""
        if marker in self._reached:
            return await self._result(self._is_finalized(), marker=marker)
        finalized = False
        while marker not in self._reached and not finalized:
            if self._cursor >= len(self._script.frames):
                break
            finalized = await self._advance_one()
        return await self._result(finalized, marker=marker)

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
        # Persist BEFORE the SSE flush, mirroring the live ``tick_once``
        # persist-then-flush ordering: a store-write failure leaves both the
        # store and the broadcaster clean, so a re-step retries with neither a
        # half-persisted verdict nor a double-emitted advisory. (#103) The flag
        # is set only after *both* the persistence and the SSE emit succeed, so
        # the once-only guarantee never trips on a partial write.
        tick = self._current_tick()
        await self._store.record_safety_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)
        await self._store.record_event(
            run_id=run_id,
            kind=RoastEventKind.ADVISORY,
            source=RoastEventSource.ADVISOR,
            payload=payload,
        )
        # SSE (live advisory panel); the store rows above back the detail-page
        # trace table the SPA renders from.
        self._service.events.emit(RoastEventKind.ADVISORY, payload)
        self._clamp_emitted = True
        self._reached.add(ReplayMarker.CLAMP)

    def _current_tick(self) -> int:
        runner = self._service.runner
        return 0 if runner is None else runner.current_tick

    def _is_finalized(self) -> bool:
        runner = self._service.runner
        return runner is not None and runner.finalized

    async def _result(
        self, finalized: bool, *, marker: ReplayMarker | None = None
    ) -> ReplayStepResult:
        phase, elapsed, tick = self._snapshot_fields()
        return ReplayStepResult(
            agent_phase=phase,
            tick=tick,
            elapsed_seconds=elapsed,
            finalized=finalized or self._is_finalized(),
            settled=True,
            last_event_id=self._service.events.last_event_id,
            run_id=self._run_id,
            persisted_point_count=await self._persisted_point_count(),
            requested_marker=None if marker is None else marker.value,
            marker_reached=marker is None or marker in self._reached,
        )

    async def _persisted_point_count(self) -> int:
        """The store-backed CHARGED-telemetry row count for the run (#338).

        The LOSSLESS settle target: the number of curve points the SPA renders
        once it has caught up, read from the same ``GET /telemetry`` snapshot the
        browser re-hydrates from (#153) — so it never depends on every SSE frame
        arriving. Counts only rows with a non-null ``charge_elapsed_seconds``:
        pre-charge preheat rows carry a null charge clock and are NOT plotted
        (``pointFromSnapshot`` drops them), so the charged count matches the
        rendered curve exactly — a settle on the full ``point_count`` would
        overshoot by the preheat lead-in and never be reached in a pre-charge
        state. ``0`` before the run starts (no run id yet).
        """
        if self._run_id is None:  # pragma: no cover — start() precedes stepping
            return 0
        series = await self._service.telemetry(self._run_id, downsample=1)
        return sum(1 for p in series.points if p.charge_elapsed_seconds is not None)

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
    use_live_post_fc_control: bool = False,
    use_live_reference_retrieval: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[RoastService, ReplaySource, RoastStore]:
    """Wire a real :class:`RoastService` + :class:`ReplaySource` for an export.

    The service is fully real (store, broadcaster, operator queue, controller
    loop disabled — ``run_loop=False`` so the source owns stepping). Returns the
    store too so the caller owns its lifecycle (``initialize`` before serving,
    ``close`` after). Shared by the CLI (``--replay``) and the tests.

    **Invariant (#495 promotion follow-up, safety-reviewer MEDIUM): replay
    reproduces a FIXED recorded trajectory; it never re-simulates under live
    control defaults.** A recorded export's telemetry drives the REAL
    controller (the design invariant this module documents at the top), so
    the deterministic post-FC RoR-taper + ceiling-guard drop
    (:class:`~roastpilot_agent.config.PostFirstCrackControl`) fires on
    replayed readings exactly as it would on live ones. The 12 Jul D88/D89
    promotion (#495) flipped that config's OWN default to ``True`` for LIVE
    roasts — but a config carrying that default (whether it is the caller's
    explicit ``AppConfig()``, or the operator's saved config file loaded by
    the CLI's ``--replay`` path, which always passes one explicitly) would
    silently let the guard auto-drop a recorded export mid-playback the
    instant a reading crosses the (now-default) ceiling, diverging from what
    the export actually recorded (confirmed: the committed
    ``cooling-complete`` fixture reaches 206 °C — its phase timeline changes
    from ``development -> cooling -> complete`` to ``pre_first_crack ->
    cooling -> complete`` with an injected guard drop under a live-default
    config). So this factory OVERRIDES ``post_first_crack_control`` to OFF
    (both flags) on whatever ``config`` resolves to — REGARDLESS of whether
    ``config`` was explicitly supplied — unless ``use_live_post_fc_control``
    is set, which is the escape hatch for a caller who deliberately wants a
    live-defaults replay (e.g. to preview how a NEW recording would behave
    under the current production defaults, not to reproduce history).

    **The identical invariant applies to same-bean reference retrieval
    (#567 Slice B design note §6.5): replay must never perform a LIVE
    reference lookup against the replaying machine's current store.** A
    replay of an old export could otherwise pick up a reference roast that
    did not exist yet (or was not yet rated) at the time the export was
    originally recorded — replaying history under data that didn't exist
    when it happened. So this factory ALSO overrides
    ``reference_curve.enabled`` to ``False`` on whatever ``config``
    resolves to, in the SAME ``model_copy`` this docstring describes above,
    regardless of what ``config`` supplied — unless
    ``use_live_reference_retrieval`` is set, mirroring
    ``use_live_post_fc_control``'s own escape hatch exactly.

    **The joint-window planner (#710 RP-C slice 1, D177) is pinned OFF
    unconditionally — no ``use_live_*`` opt-out.** ``model_copy(update=…)``
    does not re-run pydantic validators, so an operator config that is
    legitimately ``joint_window_planner.enabled=True`` (and therefore, per
    D177, has the ceiling guard on) would, after the ``post_first_crack_
    control`` pin above turns the guard off, silently hold a field
    combination the D177 validator exists to forbid. This factory also
    overrides ``joint_window_planner`` to ``JointWindowPlanner(enabled=
    False)`` in the SAME ``model_copy``, always — adding a live opt-out here
    would create exactly the invalid combination D177 forbids, so none is
    authorised.

    Args:
        export_dir: The recorded ``roast.jsonl`` export directory to replay.
        store_path: Where to create the replay's own SQLite store.
        config: The base config to replay under; defaults to ``AppConfig()``.
            Its ``post_first_crack_control`` and ``reference_curve`` sections
            are overridden per the invariants above unless the matching
            ``use_live_*`` flag is set. Its ``joint_window_planner`` section
            is always overridden to ``enabled=False`` with no opt-out.
        use_live_post_fc_control: Opt OUT of the pinned-baseline invariant —
            replay under ``config``'s own (possibly live-default) post-FC
            control settings instead. Default ``False``.
        use_live_reference_retrieval: Opt OUT of the pinned-off reference
            invariant — replay with same-bean reference retrieval running
            LIVE against the replaying machine's current store instead of
            forced off. Default ``False``. Mirrors
            ``use_live_post_fc_control``.
        sleep: The inter-tick delay coroutine the free-running
            :meth:`ReplaySource.run` awaits; defaults to :func:`asyncio.sleep`.
            Tests pass a no-op so the free-running path drives the whole
            export instantly, with the same event stream — exposed on the
            public factory so a test never reaches into the source's private
            ``_sleep`` (#103).

    Returns:
        ``(service, source, store)``.
    """
    app_config = config or AppConfig()
    controller_pins: dict[str, object] = {}
    if not use_live_post_fc_control:
        controller_pins["post_first_crack_control"] = PostFirstCrackControl(
            enabled=False, ceiling_guard_drop_enabled=False
        )
    if not use_live_reference_retrieval:
        controller_pins["reference_curve"] = ReferenceCurve(enabled=False)
    # #710 (RP-C) slice 1 / D177: always pinned off, unconditionally — no
    # ``use_live_*`` opt-out is authorised (see the docstring above). This
    # also means ``controller_pins`` is never empty any more, so (unlike
    # before #710) the ``model_copy`` below always runs, even when BOTH
    # ``use_live_*`` flags are set.
    controller_pins["joint_window_planner"] = JointWindowPlanner(enabled=False)
    app_config = app_config.model_copy(
        update={"controller": app_config.controller.model_copy(update=controller_pins)}
    )
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
        sleep=sleep,
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


class ReplayStepToRequest(BaseModel):
    """``POST /api/replay/step-to`` body: advance to an ABSOLUTE cursor tick (#338).

    The idempotent sibling of ``step`` (count-based): a retry that re-issues the
    same absolute target lands the SAME state rather than over-stepping the
    stateful replay agent."""

    tick: int = Field(ge=0)


def mount_replay_controls(app: FastAPI, source: ReplaySource) -> None:
    """Mount the gated ``/api/replay/{step,step-to,advance-to}`` control routes.

    Only the ``--step`` replay app calls this. Each route advances the real
    controller synchronously and returns the settled :class:`ReplayStepResult`
    (phase, tick, ``run_id`` + ``persisted_point_count`` for the lossless settle,
    #338). ``advance-to`` returns **404** when the requested marker never fires in
    the export, so a Playwright caller fails loud on a wrong fixture/marker instead
    of screenshotting the wrong (terminal) state. ``step-to`` is the idempotent
    absolute-cursor variant of ``step`` (retry-safe under Playwright ``retries``,
    #338)."""

    async def step(request: ReplayStepRequest) -> dict[str, Any]:
        return (await source.step(request.ticks)).to_json()

    async def step_to(request: ReplayStepToRequest) -> dict[str, Any]:
        return (await source.step_to(request.tick)).to_json()

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
    app.post("/api/replay/step-to")(step_to)
    app.post("/api/replay/advance-to")(advance_to)


async def create_replay_app(
    export_dir: Path,
    store_path: Path,
    *,
    config: AppConfig | None = None,
    use_live_post_fc_control: bool = False,
    step_mode: bool = False,
    speed: float = 1.0,
    spa_dir: Path | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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

    ``spa_dir`` (when set) serves the built SPA at ``/`` so the recorded roast
    renders in the real dashboard, mounted after the API routes exactly as the
    live serve path mounts it.

    ``use_live_post_fc_control`` is forwarded to :func:`build_replay_service`
    — see its docstring for the pinned-baseline replay invariant (#495) this
    parameter opts out of.

    ``sleep`` is the free-running inter-tick delay coroutine (default
    :func:`asyncio.sleep`); a test passes a no-op to drive the export instantly
    without reaching into the source's private ``_sleep`` (#103).
    """
    service, source, store = build_replay_service(
        export_dir,
        store_path,
        config=config,
        use_live_post_fc_control=use_live_post_fc_control,
        sleep=sleep,
    )
    source.set_speed(speed)
    # Tear down the store (and its aiosqlite worker thread) + the service if
    # bring-up fails after the store opens: a ``source.start()`` raising —
    # e.g. a controller-start error — must not leak the worker thread past the
    # event loop (no lifespan runs to clean it up, since the app is never
    # returned). (#103)
    try:
        await store.initialize()
        await source.start()
    except BaseException:
        await source.aclose()
        raise

    @contextlib.asynccontextmanager
    async def _replay_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # No recover_on_start — replay owns the (already-active) run; on shutdown
        # tear down the live loop (mirroring the live lifespan) and close the
        # store this factory owns, so no aiosqlite thread outlives the loop.
        yield
        await service.shutdown()
        await store.close()

    # The SPA mount (a catch-all at "/") must be registered AFTER the gated
    # /api/replay/* control routes, or it would shadow them; so create_app mounts
    # no SPA here and mount_spa runs last, once every /api route exists.
    app = create_app(service, lifespan=_replay_lifespan)
    if step_mode:
        mount_replay_controls(app, source)
    if spa_dir is not None and (spa_dir / "index.html").is_file():
        mount_spa(app, spa_dir)
    return app, service, source
