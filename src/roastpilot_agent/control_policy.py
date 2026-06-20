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

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import RoastPhase, RoastProfile

# The full lever range. Today every phase resolves to this, reproducing the
# gate's current 0–100 clamp exactly (the #273 "no verdict change" invariant);
# #222 narrows it per phase (e.g. pre-FC fan ceiling ~30) on this same object.
_LEVER_MIN_PERCENT = 0
_LEVER_MAX_PERCENT = 100


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

    @model_validator(mode="after")
    def _check_ranges(self) -> "PhaseControlLimits":
        """Floors must not exceed ceilings, and the drop bound the bitter one.

        A floor above its ceiling would be an empty box (no executable lever
        value); the emergency-drop bound must stay above the bitter ceiling
        (D35 §3, mirrored from :class:`SafetyLimits`).

        Returns:
            The validated limits instance.

        Raises:
            ValueError: If any floor exceeds its ceiling, or the emergency-drop
                bound is not above the bitter ceiling.
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
        return self


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
    (heat 100 / fan low to FC); #223 reads these *limits* for the post-FC drop
    box. Both build on this single source rather than duplicating the numbers.
    """

    def __init__(self, limits: SafetyLimits, profile: RoastProfile | None = None) -> None:
        """Construct the policy from the safety limits and the active profile.

        Args:
            limits: The hard safety limits (the single config source for the
                heat/fan range and the bitter / emergency-drop ceilings).
            profile: The frozen active roast profile, or ``None`` when no run is
                in progress (the limits then resolve from ``limits`` alone). The
                profile only ever *tightens* a told ceiling, never loosens it.
        """
        self._limits = limits
        self._profile = profile

    def limits_for(self, phase: RoastPhase) -> PhaseControlLimits:
        """Resolve the control box for ``phase`` from the single source.

        Today every phase resolves the full 0–100 heat/fan range, reproducing
        the safety gate's current clamp behaviour exactly (the #273 "no verdict
        change" invariant). #222 narrows this range per phase on this same
        object. The bitter ceiling is the configured hard ceiling, capped at the
        active profile's drop target when that is lower; the emergency-drop bound
        is the configured hard bound.

        Args:
            phase: The agent phase the controller is currently in.

        Returns:
            The :class:`PhaseControlLimits` box for ``phase`` — the *same*
            object the gate clamps into and the model is told about.
        """
        return PhaseControlLimits(
            heat_floor_percent=_LEVER_MIN_PERCENT,
            heat_ceiling_percent=_LEVER_MAX_PERCENT,
            fan_floor_percent=_LEVER_MIN_PERCENT,
            fan_ceiling_percent=_LEVER_MAX_PERCENT,
            bitter_ceiling_temp_c=self._bitter_ceiling_temp_c(),
            emergency_drop_temp_c=self._limits.emergency_drop_temp_c,
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
