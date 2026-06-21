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

    def limits_for(self, phase: RoastPhase) -> PhaseControlLimits:
        """Resolve the control box for ``phase`` from the single source.

        The two pre-FC phases (preheat, charge→FC) resolve a NARROWED box with a
        deterministic lever target (D35 §3/§4-A, #222): heat resolves the range
        ``[heat_target, 100]`` — the FLOOR is pinned to the deterministic target
        (so a momentum-killing cut is structurally impossible — the gate clamps
        any lower value back up) while the ceiling stays at 100. At the default
        target 100 that range collapses to the point ``[100, 100]``, but a future
        learned target below 100 (D42 §7.1) yields a genuine range; it is the
        FLOOR, not a single pinned value, that prevents the #218 cut. Fan is
        capped low (the operator's max-heat / low-fan-to-FC method). Every other
        phase resolves the full 0–100 range with no deterministic target — development
        → drop is the post-FC LLM's box (#223); the lifecycle states do not
        actuate. The bitter ceiling is the configured hard ceiling, capped at the
        active profile's drop target when that is lower; the emergency-drop bound
        is the configured hard bound.

        Args:
            phase: The agent phase the controller is currently in.

        Returns:
            The :class:`PhaseControlLimits` box for ``phase`` — the *same*
            object the gate clamps into, the model is told about, and (pre-FC)
            the controller deterministically actuates.
        """
        bitter = self._bitter_ceiling_temp_c()
        emergency = self._limits.emergency_drop_temp_c
        if phase in _PRE_FIRST_CRACK_PHASES:
            levers = self._pre_fc_levers
            return PhaseControlLimits(
                # Heat pinned high: floor == the deterministic target, so the
                # gate clamps any lower request (or a stale/odd value) back up to
                # the target — the heat 70→40→20→0 pre-FC crash (#218) cannot
                # recur. Ceiling stays full 100 (steady high heat, operator
                # method; the deferred late-Maillard trim, D36/#228, would lower
                # the floor here when FC-ETA exists).
                heat_floor_percent=levers.heat_target_percent,
                heat_ceiling_percent=_LEVER_MAX_PERCENT,
                # Fan capped low: floor 0, ceiling the configured low value (~30)
                # — the operator's low-fan-to-browning method.
                fan_floor_percent=_LEVER_MIN_PERCENT,
                fan_ceiling_percent=levers.fan_ceiling_percent,
                bitter_ceiling_temp_c=bitter,
                emergency_drop_temp_c=emergency,
                heat_target_percent=levers.heat_target_percent,
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
