"""The single source of truth for per-phase control limits (D35 §8.2/8.3, #273).

D35 splits control at first crack: a deterministic pre-FC controller and a
post-FC LLM, both operating *inside a hard box*. The box — the heat/fan range,
the ≤196 °C indicated bitter / drop ceiling, and the emergency-drop bound — must
be defined **once** and feed **both** the prompt context the model reasons
inside (``AdvisorContext``) **and** the harness execute-or-not gate
(``safety.SafetyPolicy``). *Told must equal enforced.*

Two copies of the numbers (one in the prompt, one in the gate) would drift into
**silent clamping the model cannot reason about** — the exact #218 incoherence
D35 exists to prevent. So :class:`RoastControlPolicy` is the ONE place the limits
resolve, and nothing downstream keeps a second copy.

This module imports only :mod:`roastpilot_agent.config` and
:mod:`roastpilot_agent.models` (both lower in the dependency graph), so
``safety.py``, ``advisor.py``, and ``controller.py`` can all consume it without
an import cycle.

Scope of #273: introduce the policy and resolve the **limit box** from the one
source. The pre-FC *lever* values (heat 100 / fan low, the n8n decision-tree
targets) are #222's job; this object is deliberately structured so #222 adds that
lever resolution as another method on the same policy, with no second copy of the
numbers. Today every phase resolves the full 0–100 heat/fan range — exactly the
gate's current behaviour — so wiring the gate to read the box here is a verdict
no-op (#273's invariant), and #222 narrows the range per phase.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator

from roastpilot_agent.config import PreFirstCrackLevers, SafetyLimits
from roastpilot_agent.models import RoastPhase, RoastProfile

# The full lever range. Phases the deterministic pre-FC policy does not own
# (development → drop, cooling, the lifecycle states) resolve to this full
# 0–100 box, reproducing the gate's pre-#222 clamp exactly. #222 narrows the
# two pre-FC phases (PREHEATING / ROASTING_PRE_FIRST_CRACK) on this same object.
_LEVER_MIN_PERCENT = 0
_LEVER_MAX_PERCENT = 100

# The pre-first-crack phases the deterministic lever policy owns (D35 §3/§4-A):
# preheat and charge→FC. In both the controller sets heat/fan from the policy
# every tick and the free-form advisor is NOT consulted (#222). Every other
# phase resolves the full 0–100 box and carries no deterministic target (the
# post-FC LLM owns development → drop; #223).
_PRE_FIRST_CRACK_PHASES: frozenset[RoastPhase] = frozenset(
    {RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK}
)


@dataclass(frozen=True)
class TrimSignal:
    """The live anticipatory-trim inputs the controller feeds the policy (#327).

    Carries the values the deterministic late-Maillard heat trim is keyed on: the
    current bean temperature, the #229 predicted-FC ETA, and the controller's
    per-run **latch** state. Bean-temp + ETA are read at the tick; the policy
    decides whether the trim window is open (see
    :meth:`RoastControlPolicy.trim_window_open` /
    :meth:`RoastControlPolicy.limits_for`). When the controller cannot resolve a
    signal (no curve yet, post-FC, no warming slope) it passes ``None`` for
    ``first_crack_eta_seconds`` (or no signal at all) and the policy FAILS CLOSED
    to the flat #222 floor.

    The **latch** (#327, hysteresis) makes the trim monotonic within a pre-FC
    run: the controller sets ``latched`` once the window has *first* opened (on a
    clean ETA + bean-floor signal) and keeps it set for the rest of the pre-FC
    phase. A latched signal keeps the trim engaged even if the noisy linear FC-ETA
    momentarily bounces back above the window, so the deterministic heat does not
    oscillate 100↔trim↔100 (the #218 lever-thrash anti-pattern). The latch resets
    per run/preheat and the trim ends naturally when FC moves the phase out of
    pre-FC. The latch NEVER engages the trim on its own — the *first* engagement
    still requires the full window precondition (valid ETA + bean ≥ floor), so a
    garbage ETA never latches.

    Side-effect free and immutable. All temperatures are Celsius.
    """

    #: The current bean temperature (°C) for the bean-temp window guard.
    bean_temp_c: float
    #: The #229 predicted seconds until first crack, or ``None`` when no estimate
    #: is warranted (too little curve, not warming, already in the FC band). A
    #: ``None`` ETA fails a *fresh* trim engagement closed to the flat floor.
    first_crack_eta_seconds: float | None
    #: The controller's per-run latch: ``True`` once the trim window has already
    #: opened this pre-FC phase. Keeps the trim engaged through an ETA bounce
    #: (hysteresis); reset per run/preheat. Defaults ``False`` (no prior engage).
    latched: bool = False


class PhaseControlLimits(BaseModel):
    """The control box for one :class:`~roastpilot_agent.models.RoastPhase`.

    Every value is resolved from the single :class:`RoastControlPolicy`; nothing
    downstream recomputes or re-stores them. The heat/fan range is the box the
    gate clamps a command into (``safety.SafetyPolicy.evaluate_command``) and the
    box the model is told it may move inside (``advisor.AdvisorContext``) — the
    two are the *same* object's output, which is the told==enforced proof.

    The temperature ceilings (``bitter_ceiling_temp_c`` / ``emergency_drop_temp_c``)
    are *told* limits surfaced for the post-FC LLM (#223) to reason inside; #273
    does not turn them into a new safety verdict (the enforced hard ceiling stays
    ``SafetyLimits.max_bean_temp_c``). They are carried here so that when #223
    wires the drop box, it reads the same single source the model was told.

    All temperatures are Celsius (the project-wide invariant).
    """

    heat_floor_percent: int = Field(ge=0, le=100)
    heat_ceiling_percent: int = Field(ge=0, le=100)
    fan_floor_percent: int = Field(ge=0, le=100)
    fan_ceiling_percent: int = Field(ge=0, le=100)
    bitter_ceiling_temp_c: float = Field(gt=0)
    emergency_drop_temp_c: float = Field(gt=0)
    #: The deterministic heat/fan target the controller actuates every tick in
    #: this phase (D35 §3/§4-A, #222), or ``None`` when the phase carries no
    #: deterministic lever (post-FC: the LLM decides; lifecycle states: no
    #: actuation). Set together — both present (a pre-FC phase) or both ``None``.
    #: When present each target sits *inside* its own heat/fan box (a model
    #: validator pins this), so the deterministic write is never itself clamped.
    heat_target_percent: int | None = Field(default=None, ge=0, le=100)
    fan_target_percent: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _check_ranges(self) -> "PhaseControlLimits":
        """Floors must not exceed ceilings, the drop bound the bitter one, and
        any deterministic target must sit inside its own box.

        A floor above its ceiling would be an empty box (no executable lever
        value); the emergency-drop bound must stay above the bitter ceiling
        (D35 §3, mirrored from :class:`SafetyLimits`). The deterministic lever
        targets (#222) are all-or-nothing — both set or both ``None`` — and each
        must lie within its own heat/fan box so the controller's deterministic
        write passes the gate as ALLOW, never silently clamped (told == enforced
        for the deterministic path too).

        Returns:
            The validated limits instance.

        Raises:
            ValueError: If any floor exceeds its ceiling, the emergency-drop
                bound is not above the bitter ceiling, the targets are set
                inconsistently, or a target falls outside its box.
        """
        if self.heat_floor_percent > self.heat_ceiling_percent:
            raise ValueError(
                "heat_floor_percent must not exceed heat_ceiling_percent "
                f"({self.heat_floor_percent} > {self.heat_ceiling_percent})"
            )
        if self.fan_floor_percent > self.fan_ceiling_percent:
            raise ValueError(
                "fan_floor_percent must not exceed fan_ceiling_percent "
                f"({self.fan_floor_percent} > {self.fan_ceiling_percent})"
            )
        if self.emergency_drop_temp_c <= self.bitter_ceiling_temp_c:
            raise ValueError(
                "emergency_drop_temp_c must be above bitter_ceiling_temp_c "
                f"({self.emergency_drop_temp_c} <= {self.bitter_ceiling_temp_c})"
            )
        if (self.heat_target_percent is None) != (self.fan_target_percent is None):
            raise ValueError(
                "heat_target_percent and fan_target_percent must be set together "
                "(both a deterministic pre-FC phase or both None)"
            )
        if self.heat_target_percent is not None and not (
            self.heat_floor_percent <= self.heat_target_percent <= self.heat_ceiling_percent
        ):
            raise ValueError(
                "heat_target_percent must lie within the heat box "
                f"({self.heat_floor_percent}–{self.heat_ceiling_percent}): "
                f"{self.heat_target_percent}"
            )
        if self.fan_target_percent is not None and not (
            self.fan_floor_percent <= self.fan_target_percent <= self.fan_ceiling_percent
        ):
            raise ValueError(
                "fan_target_percent must lie within the fan box "
                f"({self.fan_floor_percent}–{self.fan_ceiling_percent}): "
                f"{self.fan_target_percent}"
            )
        return self

    @property
    def has_deterministic_target(self) -> bool:
        """Whether this phase carries a deterministic heat/fan lever target.

        ``True`` only for the pre-FC phases the controller actuates from the
        policy (#222). The two target fields are validated all-or-nothing, so
        ``heat_target_percent`` alone is a sufficient discriminator.

        Returns:
            ``True`` when both lever targets are set, ``False`` otherwise.
        """
        return self.heat_target_percent is not None


class RoastControlPolicy:
    """The ONE place per-phase control limits are defined (D35 §8.3, #273).

    Resolves, per :class:`~roastpilot_agent.models.RoastPhase`, the live control
    box: the heat/fan floor + ceiling, the ≤196 °C indicated bitter / drop
    ceiling, and the emergency-drop bound. The same resolved
    :class:`PhaseControlLimits` feeds **both** the advisor context (what the model
    is told) **and** the safety gate (what the harness enforces) — there is no
    second copy of the numbers anywhere (told == enforced).

    The temperature ceilings are profile-aware: ``bitter_ceiling_temp_c`` is the
    hard software ceiling (``SafetyLimits``) but is *capped at the profile's drop
    target* when that target is lower, so the model is never told it may push
    above the roast it was configured for. The emergency-drop bound stays the
    hard ``SafetyLimits`` value (the last-resort bound is profile-independent).

    Deterministic and side-effect free: it reads the configured
    :class:`SafetyLimits` and the optional active :class:`RoastProfile` only. The
    policy is constructed once per run (the profile is frozen at run start) and
    consulted every tick.

    #222 extends this same object with the pre-FC deterministic *lever* targets
    (heat 100 / fan low to FC) and the narrowed pre-FC box that contains them;
    #223 reads these *limits* for the post-FC drop box. Both build on this single
    source rather than duplicating the numbers.
    """

    def __init__(
        self,
        limits: SafetyLimits,
        profile: RoastProfile | None = None,
        *,
        pre_fc_levers: PreFirstCrackLevers | None = None,
    ) -> None:
        """Construct the policy from the safety limits, profile, and pre-FC levers.

        Args:
            limits: The hard safety limits (the single config source for the
                heat/fan range and the bitter / emergency-drop ceilings).
            profile: The frozen active roast profile, or ``None`` when no run is
                in progress (the limits then resolve from ``limits`` alone). The
                profile only ever *tightens* a told ceiling, never loosens it.
            pre_fc_levers: The deterministic pre-FC heat/fan lever parameters
                (#222) — the operator's proven n8n defaults (heat 100 / fan low)
                unless overridden. ``None`` uses :class:`PreFirstCrackLevers`
                defaults; the parameters are profile/config-driven so a learned
                plan (D42 §7.1) can later supply them without a code change.
        """
        self._limits = limits
        self._profile = profile
        self._pre_fc_levers = pre_fc_levers if pre_fc_levers is not None else PreFirstCrackLevers()

    def limits_for(
        self, phase: RoastPhase, *, trim_signal: TrimSignal | None = None
    ) -> PhaseControlLimits:
        """Resolve the control box for ``phase`` from the single source.

        The two pre-FC phases (preheat, charge→FC) resolve a NARROWED box with a
        deterministic lever target (D35 §3/§4-A, #222): heat resolves the range
        ``[heat_target, 100]`` — the FLOOR is pinned to the deterministic target
        (so a momentum-killing cut is structurally impossible — the gate clamps
        any lower value back up) while the ceiling stays at 100. At the default
        target 100 that range collapses to the point ``[100, 100]``, but a future
        learned target below 100 (D42 §7.1) yields a genuine range; it is the
        FLOOR, not a single pinned value, that prevents the #218 cut. Fan is
        capped low (the operator's max-heat / low-fan-to-FC method).

        **Anticipatory heat trim (#327).** When ``trim_signal`` opens the
        late-Maillard → FC window (see :meth:`_trim_engaged`) the pre-FC heat
        floor AND target are lowered to the configured trim level (a moderate
        ~60–70 % reduction, not a crash) so the env cools and the RoR bends into
        FC before the drop ceiling — roast 3 proved the flat 100 floor overshoots.
        The trim is a strict reduction (a config validator pins
        ``trim_heat_percent <= heat_target_percent``), so the floor never rises
        and FC is never delayed by added heat. Outside the window — and whenever
        the FC-ETA is unknown / no signal is supplied — it FAILS CLOSED to the
        flat #222 floor (the always-on guarantee FC still arrives, §8.4). The fan
        box and target are unchanged by the trim (fan stays at the floor; the
        plan's "fan controlled", not raised — raising fan pre-FC crashes RoR into
        the crack, the #218 anti-pattern).

        Every other phase resolves the full 0–100 range with no deterministic
        target — development → drop is the post-FC LLM's box (#223); the lifecycle
        states do not actuate. The bitter ceiling is the configured hard ceiling,
        capped at the active profile's drop target when that is lower; the
        emergency-drop bound is the configured hard bound.

        Args:
            phase: The agent phase the controller is currently in.
            trim_signal: The live bean-temp + FC-ETA the late-Maillard trim is
                keyed on, or ``None`` (the controller cannot resolve one, or the
                caller does not want the trim — both fail closed to the flat
                floor). Ignored outside the pre-FC phases.

        Returns:
            The :class:`PhaseControlLimits` box for ``phase`` — the *same*
            object the gate clamps into, the model is told about, and (pre-FC)
            the controller deterministically actuates.
        """
        bitter = self._bitter_ceiling_temp_c()
        emergency = self._limits.emergency_drop_temp_c
        if phase in _PRE_FIRST_CRACK_PHASES:
            levers = self._pre_fc_levers
            # The deterministic heat the controller holds this tick: the trim
            # level while the late-Maillard window is open (#327), else the flat
            # #222 floor target. The floor is pinned to whichever heat is active
            # so the gate clamps any lower value back up to it (no cut below the
            # active level), and the trim is a strict reduction so the floor never
            # rises above the #222 target.
            heat = (
                levers.late_maillard_trim.trim_heat_percent
                if self._trim_engaged(trim_signal)
                else levers.heat_target_percent
            )
            return PhaseControlLimits(
                heat_floor_percent=heat,
                heat_ceiling_percent=_LEVER_MAX_PERCENT,
                # Fan capped low: floor 0, ceiling the configured low value (~30)
                # — the operator's low-fan-to-browning method. The trim leaves fan
                # at the floor (plan §3 "fan controlled", not raised).
                fan_floor_percent=_LEVER_MIN_PERCENT,
                fan_ceiling_percent=levers.fan_ceiling_percent,
                bitter_ceiling_temp_c=bitter,
                emergency_drop_temp_c=emergency,
                heat_target_percent=heat,
                fan_target_percent=levers.fan_target_percent,
            )
        return PhaseControlLimits(
            heat_floor_percent=_LEVER_MIN_PERCENT,
            heat_ceiling_percent=_LEVER_MAX_PERCENT,
            fan_floor_percent=_LEVER_MIN_PERCENT,
            fan_ceiling_percent=_LEVER_MAX_PERCENT,
            bitter_ceiling_temp_c=bitter,
            emergency_drop_temp_c=emergency,
        )

    def trim_window_open(self, trim_signal: TrimSignal | None) -> bool:
        """Whether a *fresh* late-Maillard trim engagement's window is open (#327).

        The full engage PRECONDITION — the gate the controller's per-run latch is
        set from. ``True`` only when ALL hold:

        * the trim is enabled in config;
        * a ``trim_signal`` is supplied with a *known* FC-ETA (a ``None`` ETA is
          the fail-closed case: no curve yet, post-FC, or no warming slope);
        * the FC-ETA is positive and at or below the configured window
          (``window_fc_eta_seconds``) — i.e. FC is predicted within the window,
          so we are in late Maillard;
        * the bean is at or above ``min_bean_temp_c`` — a guard against a noisy
          RoR projecting a spurious near-term FC early in the roast.

        Latch-independent (it ignores ``trim_signal.latched``): this is what makes
        the FIRST engagement require a clean signal, so a garbage ETA never
        latches. Any missing/unknown input ⇒ ``False`` ⇒ the flat #222 floor (fail
        closed, §8.4).

        Args:
            trim_signal: The live bean-temp + FC-ETA, or ``None``.

        Returns:
            ``True`` when the fresh-engage window is open, ``False`` otherwise.
        """
        trim = self._pre_fc_levers.late_maillard_trim
        if not trim.enabled or trim_signal is None:
            return False
        eta = trim_signal.first_crack_eta_seconds
        # `not (0.0 < eta <= window)` is True for None, NaN, ≤0, and >window.
        # NaN comparisons all return False in Python, so NaN passes the old
        # `eta <= 0.0 or eta > window` guards — this form fails closed for NaN too.
        if eta is None or not (0.0 < eta <= trim.window_fc_eta_seconds):
            return False
        return trim_signal.bean_temp_c >= trim.min_bean_temp_c

    def _trim_engaged(self, trim_signal: TrimSignal | None) -> bool:
        """Whether the deterministic late-Maillard heat trim is active (#327).

        Engaged when the trim is enabled AND either the fresh-engage window is
        open (:meth:`trim_window_open`) OR the controller's per-run latch is set
        (``trim_signal.latched``). The latch is the HYSTERESIS: once the window
        has opened this pre-FC phase the trim stays engaged through a momentary
        ETA bounce above the window, so the deterministic heat does not oscillate
        100↔trim↔100 (the #218 lever-thrash). The latch alone never engages the
        trim on a degenerate signal — a latched signal is only produced by the
        controller AFTER a clean :meth:`trim_window_open`, and the ``enabled``
        check here still gates it (a config-disabled trim is never engaged).

        Args:
            trim_signal: The live bean-temp + FC-ETA + latch, or ``None``.

        Returns:
            ``True`` when the trim is engaged this tick, ``False`` otherwise.
        """
        if trim_signal is None or not self._pre_fc_levers.late_maillard_trim.enabled:
            return False
        return trim_signal.latched or self.trim_window_open(trim_signal)

    def _bitter_ceiling_temp_c(self) -> float:
        """The told ≤196 °C bitter / drop ceiling, capped at the profile target.

        The hard software ceiling from :class:`SafetyLimits`, lowered to the
        active profile's ``target_drop_temp_c`` when that target is lower (a
        lighter roast must never be told it may push to 196 °C). Returns the hard
        ceiling unchanged when there is no profile or its target is higher.
        """
        ceiling = self._limits.bitter_ceiling_temp_c
        if self._profile is not None:
            ceiling = min(ceiling, self._profile.target_drop_temp_c)
        return ceiling
