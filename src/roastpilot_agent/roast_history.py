"""Per-tick context builder for the D35 post-FC control loop (D40.3 + D40.5).

This module assembles the *user-message* context the control-loop model reasons
on: the roast-so-far telemetry curve up to time *t*, the development time and
Development Time Ratio (DTR), and the model's own prior recommendations encoded
as a decision trace. It is **context assembly only** — it never actuates
hardware, never touches the safety policy, and never changes a verdict. The
controller still owns the loop and the advisor still returns a typed
:class:`~roastpilot_agent.advisor.RoastDecision`; this story (#275) only builds
the context. Wiring it into the live post-FC consult loop is #276.

Design (D40 §8.5, operator-ratified):

- **"Reference curve" = THIS roast's own telemetry up to time t** (bean / env /
  heat / fan, + RoR), NOT an ideal/target curve — an ideal-curve / cross-roast
  feedback loop is roastpilot-cloud scope (D29), out of M1. The model sees *how
  today's responses have actually landed* and can correct its own trajectory.
- **Bounded payload.** The curve is a **recent full-resolution window** plus a
  **milestone summary** (turning point / recovery / drying-end / first crack),
  never a raw 100-plus-point dump. The window and trace sizes are explicit named
  constants (the controller's :class:`~roastpilot_agent.config.ControllerConfig`
  carries the live, configurable values), and the bound is tested.
- **Decisions encoded.** The trace carries the model's own recent
  recommendations (heat / fan / drop / confidence) so it can see and correct its
  trajectory — the direct fix for the #218 thrash (the model had no memory of its
  own moves).
- **Only validation-supported derived features** (#229,
  ``docs/advisor/curve-feature-validation-2026-06-18.md``): FC-ETA is **KEEP** (a
  pre-FC anticipation trigger); the RoR-crash signature is **absent** on our data
  and is not computed; turning point is carried as a **display-only milestone**
  (a charge-temperature proxy), never as a control predictor. DTR is the existing
  #219/#220/#235/#239 clock, reused here — not reinvented.

All temperatures are Celsius.
"""

from collections import deque
from enum import Enum

from pydantic import BaseModel, Field

# --- Payload-bound named constants (the tested defaults) ----------------------
#
# The controller's ControllerConfig carries the LIVE, configurable values
# (curve_window_samples / decision_trace_entries); these are the named defaults
# the builder falls back to and the bound the tests pin. Keeping them as module
# constants makes the bound explicit and importable (acceptance: "make the
# window/summary sizes explicit").

#: Recent full-resolution telemetry samples carried in the curve window. At the
#: 1 s tick this is the last ~60 s of full-resolution curve — enough for the
#: model to read the current RoR trend and how its last moves landed, without a
#: raw whole-roast dump (which can run 600-plus points on a 10 min roast).
DEFAULT_CURVE_WINDOW_SAMPLES = 60

#: Recent advisor recommendations carried in the decision trace. At the ~5 s
#: post-FC consult cadence this is the last ~1 min of the model's own moves —
#: enough to see and damp a direction flip-flop (#218) without unbounded growth.
DEFAULT_DECISION_TRACE_ENTRIES = 12


class RoastMilestoneKind(Enum):
    """A landmark on the roast-so-far curve (display + reasoning context).

    Plain ``Enum``, never ``StrEnum`` (D15 house rule): the milestone kind is
    context only and carries no control authority, but the repo's enums are
    plain so a string comparison stays a pyright-strict error. The four kinds
    are the curve landmarks the operator and the model reason about (D40.3):

    - ``TURNING_POINT`` — the post-charge bean-temperature minimum. Carried as a
      **display-only** landmark: #229 found it is a charge-temperature proxy
      (corr 0.979 with charge temp), so it is shown, never used as a predictor.
    - ``RECOVERY`` — the bean RoR over the window just after the turning point;
      the one turning-point-family metric that survived the #229 confound check
      (a charge-independent early-pace signal), kept cautiously.
    - ``DRYING_END`` — the drying-phase boundary the controller's phase model
      crosses on the way to first crack (shown as roast structure).
    - ``FIRST_CRACK`` — the detected first crack; the development clock's origin.
    """

    TURNING_POINT = "turning_point"
    RECOVERY = "recovery"
    DRYING_END = "drying_end"
    FIRST_CRACK = "first_crack"


class RoastCurveSample(BaseModel):
    """One full-resolution point on the roast-so-far telemetry curve.

    A single tick's reading plus the levers the controller had commanded at that
    tick, so the model sees the *paired* (action, response) history — how its
    heat/fan moves landed in the bean/env temperature and RoR. Temperatures are
    Celsius; heat/fan are 0-100 percent duty (not temperatures). This is data
    only and never reaches an MCP write path.
    """

    elapsed_since_charge_seconds: float
    """Seconds since the debounced charge/T0 instant (the DTR-clock reference,
    #219). May be negative for a pre-charge (preheat) sample, mirroring the
    charge-referenced clock the advisor reasons on."""
    bean_temp_c: float
    env_temp_c: float
    heat_percent: int = Field(ge=0, le=100)
    fan_percent: int = Field(ge=0, le=100)
    bean_ror_c_per_min: float | None = None
    env_ror_c_per_min: float | None = None


class RoastMilestone(BaseModel):
    """A landmark on the roast-so-far curve (the milestone summary entry).

    Pairs a :class:`RoastMilestoneKind` with when it occurred (charge-referenced
    seconds) and the bean temperature there, plus an optional scalar ``value``
    for kinds that carry one (e.g. the recovery RoR in °C/min). Context only —
    no control authority. Temperatures are Celsius.
    """

    kind: RoastMilestoneKind
    elapsed_since_charge_seconds: float
    bean_temp_c: float
    value: float | None = None
    """An optional scalar for the kind (e.g. ``RECOVERY`` carries the post-TP
    bean RoR in °C/min). ``None`` for kinds that are purely a (time, temp)
    landmark (turning point, drying end, first crack)."""


class DecisionTraceEntry(BaseModel):
    """One of the model's own prior recommendations, encoded for the next tick.

    The recommendation history (heat / fan / drop / confidence per past consult)
    so the model can see and correct its own trajectory — the #218 anti-thrash
    fix. heat/fan are the *recommended* 0-100 percent duty (what the model asked
    for), not the clamped value the gate applied: the trace is the model's own
    move history, so it must reflect what the model itself proposed.
    """

    elapsed_since_charge_seconds: float
    target_heat: int = Field(ge=0, le=100)
    target_fan: int = Field(ge=0, le=100)
    should_drop: bool
    confidence: float = Field(ge=0.0, le=1.0)


class PerTickContextPayload(BaseModel):
    """The bounded per-tick context payload (D40.3 + D40.5).

    The assembled, bounded context the post-FC loop (#276) will carry in its
    user message: the recent full-resolution curve window, the milestone
    summary, the model's own decision trace, the development time + DTR, and the
    validation-supported FC-ETA. It is data only; it never reaches an MCP write
    path and never participates in a safety evaluation.

    The curve is deliberately a **window + summary**, never a raw whole-roast
    dump: :attr:`curve_window` is capped at the builder's configured window size
    and :attr:`decision_trace` at the configured trace size, so the payload stays
    bounded however long the roast runs.
    """

    curve_window: list[RoastCurveSample] = Field(default_factory=list[RoastCurveSample])
    """The recent full-resolution telemetry window (most recent last), bounded by
    the builder's window size."""
    milestones: list[RoastMilestone] = Field(default_factory=list[RoastMilestone])
    """The milestone summary (turning point / recovery / drying-end / first
    crack) — the compact stand-in for the older curve the window drops."""
    decision_trace: list[DecisionTraceEntry] = Field(default_factory=list[DecisionTraceEntry])
    """The model's own recent recommendations (most recent last), bounded by the
    builder's trace size (#218)."""
    development_elapsed_seconds: float | None = None
    """Development time: seconds since first crack (``None`` before FC). A
    distinct value from :attr:`development_time_ratio` — duration, not a share."""
    development_time_ratio: float | None = None
    """DTR: development time as a share (0-1) of the charge-referenced roast
    clock (``None`` before FC). The existing #219/#220 clock, reused — the
    second of the two distinct development values the acceptance requires."""
    first_crack_eta_seconds: float | None = None
    """FC-ETA: predicted seconds until first crack from RoR extrapolation
    (``None`` once FC is detected, or when there is not yet enough curve to
    project). A pre-FC anticipation trigger only (#229 KEEP), never a lever move
    on its own."""


def estimate_first_crack_eta_seconds(
    curve: list[RoastCurveSample],
    *,
    fc_target_bean_temp_c: float,
    min_samples: int = 5,
) -> float | None:
    """Estimate seconds until first crack by extrapolating the recent bean RoR.

    The #229-validated FC-ETA (``KEEP`` as a pre-FC anticipation trigger): from
    the most recent curve samples, take the current bean temperature and a
    linear bean-temperature slope (°C/s) and project the time to reach
    ``fc_target_bean_temp_c`` (the profile's FC band target). It is intentionally
    a simple linear extrapolation — #229 validated this naive projection lands
    within roughly half the detector-lag window by ~90 s out — and it is a
    *trigger to start anticipating*, not a fan/heat move on its own (the
    deterministic pre-FC floor owns the levers, per the 16 Jun negative case).

    Returns ``None`` (no estimate) when there are too few samples to fit a slope,
    when the bean is not warming (a non-positive slope can never reach the
    target), or when the bean is already at/above the target (FC is imminent or
    here — the detector, not an ETA, owns that). All temperatures are Celsius.

    Args:
        curve: The roast-so-far curve window (most recent last).
        fc_target_bean_temp_c: The FC-band bean temperature to project to.
        min_samples: The minimum samples needed to fit a slope. Defaults to 5
            (the #229 method's floor).

    Returns:
        The projected seconds until first crack, or ``None`` when no estimate is
        warranted.
    """
    if len(curve) < min_samples:
        return None
    recent = curve[-min_samples:]
    first, last = recent[0], recent[-1]
    span_seconds = last.elapsed_since_charge_seconds - first.elapsed_since_charge_seconds
    if span_seconds <= 0.0:
        return None
    slope_c_per_s = (last.bean_temp_c - first.bean_temp_c) / span_seconds
    if slope_c_per_s <= 0.0:
        # Not warming — a flat/falling curve cannot reach the FC target.
        return None
    remaining_c = fc_target_bean_temp_c - last.bean_temp_c
    if remaining_c <= 0.0:
        # Already at/through the FC band — FC is the detector's call, not an ETA.
        return None
    return remaining_c / slope_c_per_s


class RoastHistory:
    """Accumulates the roast-so-far curve, milestones, and decision trace.

    The controller owns one of these per run. Each tick it records the telemetry
    sample + the levers it commanded (:meth:`record_sample`); each consult it
    records the model's recommendation (:meth:`record_decision`); the phase
    model arms milestones (:meth:`record_milestone`). :meth:`build_payload`
    then assembles the bounded per-tick context.

    Bounding is structural: :attr:`_curve` and :attr:`_decisions` are bounded
    ``deque`` s sized at construction, so a long roast can never grow the payload
    past the configured window / trace sizes. The milestone list is bounded by
    its nature (four kinds, recorded once each). This object is pure state +
    assembly — it never calls MCP, never evaluates safety, and holds no control
    authority.
    """

    def __init__(
        self,
        *,
        curve_window_samples: int = DEFAULT_CURVE_WINDOW_SAMPLES,
        decision_trace_entries: int = DEFAULT_DECISION_TRACE_ENTRIES,
    ) -> None:
        """Initialise an empty history with explicit payload bounds.

        Args:
            curve_window_samples: Maximum full-resolution samples retained in the
                curve window. Defaults to :data:`DEFAULT_CURVE_WINDOW_SAMPLES`.
            decision_trace_entries: Maximum recommendations retained in the
                decision trace. Defaults to :data:`DEFAULT_DECISION_TRACE_ENTRIES`.

        Raises:
            ValueError: If either bound is not a positive integer.
        """
        if curve_window_samples < 1:
            raise ValueError("curve_window_samples must be >= 1")
        if decision_trace_entries < 1:
            raise ValueError("decision_trace_entries must be >= 1")
        self._curve_window_samples = curve_window_samples
        self._decision_trace_entries = decision_trace_entries
        self._curve: deque[RoastCurveSample] = deque(maxlen=curve_window_samples)
        self._decisions: deque[DecisionTraceEntry] = deque(maxlen=decision_trace_entries)
        self._milestones: dict[RoastMilestoneKind, RoastMilestone] = {}

    @property
    def curve_window_samples(self) -> int:
        """The configured full-resolution curve-window bound."""
        return self._curve_window_samples

    def curve_window(self) -> list[RoastCurveSample]:
        """Return the current curve-window samples (newest last) as a list.

        A copy of the bounded window — callers (e.g. the FC-ETA estimator) read
        it without holding the internal deque. Never longer than
        :attr:`curve_window_samples`.
        """
        return list(self._curve)

    @property
    def decision_trace_entries(self) -> int:
        """The configured decision-trace bound."""
        return self._decision_trace_entries

    def reset(self) -> None:
        """Clear all accumulated history (a new run / preheat starts fresh)."""
        self._curve.clear()
        self._decisions.clear()
        self._milestones.clear()

    def record_sample(self, sample: RoastCurveSample) -> None:
        """Append a full-resolution curve sample (the bounded deque drops the
        oldest once full)."""
        self._curve.append(sample)

    def record_decision(self, entry: DecisionTraceEntry) -> None:
        """Append one of the model's recommendations to the decision trace (the
        bounded deque drops the oldest once full)."""
        self._decisions.append(entry)

    def record_milestone(self, milestone: RoastMilestone) -> None:
        """Record a curve landmark, keeping the FIRST occurrence of each kind.

        Milestones are one-shot: the turning point, recovery, drying end, and
        first crack each happen once, so a later call for an already-recorded
        kind is ignored (the first crossing is the landmark). This makes the
        controller's per-tick arming idempotent.

        Args:
            milestone: The landmark to record.
        """
        self._milestones.setdefault(milestone.kind, milestone)

    def has_milestone(self, kind: RoastMilestoneKind) -> bool:
        """Return whether a milestone of ``kind`` has already been recorded."""
        return kind in self._milestones

    def milestones(self) -> list[RoastMilestone]:
        """Return the recorded milestones ordered by occurrence time."""
        return sorted(self._milestones.values(), key=lambda m: m.elapsed_since_charge_seconds)

    def decision_trace(self) -> list[DecisionTraceEntry]:
        """Return the decision trace (newest last) as a list copy."""
        return list(self._decisions)

    def build_payload(
        self,
        *,
        development_elapsed_seconds: float | None,
        development_time_ratio: float | None,
        first_crack_eta_seconds: float | None,
    ) -> PerTickContextPayload:
        """Assemble the bounded per-tick context payload.

        The curve window and decision trace are returned newest-last; the
        milestone summary is ordered by occurrence time. The development time,
        DTR, and FC-ETA are passed in by the controller (it owns the
        #219/#220/#235/#239 clocks and the profile's FC target) — this method
        does not recompute them.

        Args:
            development_elapsed_seconds: Seconds since first crack (``None``
                before FC) — the development *time* (duration).
            development_time_ratio: Development time as a share (0-1) of the
                charge-referenced roast clock (``None`` before FC) — the DTR.
            first_crack_eta_seconds: The FC-ETA (``None`` post-FC or when no
                estimate is warranted).

        Returns:
            The bounded :class:`PerTickContextPayload`.
        """
        return PerTickContextPayload(
            curve_window=self.curve_window(),
            milestones=self.milestones(),
            decision_trace=self.decision_trace(),
            development_elapsed_seconds=development_elapsed_seconds,
            development_time_ratio=development_time_ratio,
            first_crack_eta_seconds=first_crack_eta_seconds,
        )
