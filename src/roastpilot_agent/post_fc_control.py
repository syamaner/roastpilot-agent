"""Deterministic post-first-crack RoR-taper PI control loop (D82/D88, #405 Slice B).

This module implements the **algorithm only** — a pure, stateful, side-effect
free PI controller that holds a DECLINING bean rate-of-rise (RoR) taper post-
first-crack by adjusting the heat lever. #405 Slice B1 built the original
algorithm; #405 Slice B (D88) replaced its fixed-band setpoint with a taper
anchored to the MEASURED engagement RoR after a hardware A/B (roasts 9/10,
``docs/analysis/2026-07-09-roast9-10-postfc-ab.md``) showed the fixed-band law
(D83) actuating a runaway heat climb (72→91 %) while the advisor recommended
0 % — the fixed target (8.0 °C/min) sat ABOVE the measured engagement RoR
(6.1 °C/min), so the loop read "too slow" from tick one. This module does not
call :mod:`roastpilot_agent.controller`, :mod:`roastpilot_agent.safety`, or
:mod:`roastpilot_agent.control_policy`. Slice B2
(:meth:`roastpilot_agent.controller.RoastController._apply_deterministic_post_fc_levers`)
wires :class:`PostFcRorController` into the controller's DEVELOPMENT-phase
tick, builds the safety box from its actuated output (never an undamped
setpoint — the #412 told==enforced control-path rule), and routes every
resulting heat command through the existing safety gate exactly like the
advisor's commands are routed today, all behind the
``PostFirstCrackControl.enabled`` flag (default ``False``). **Nothing in this
module writes to a roaster.** ``PostFcRorController`` never imports
:mod:`roastpilot_agent.mcp_client` and must never be wired to it directly.

Design (D88): a PI controller (proportional + integral, no derivative term) on
a smoothed (EMA) RoR signal, with conditional-integration anti-windup — this
machinery is unchanged from D83. What D88 replaces is the SETPOINT SOURCE and
the OUTPUT CEILING:

* **Setpoint taper.** At engagement (:meth:`reset`) the caller supplies the
  measured RoR and heat the roast actually held at that instant. The taper
  target ``r(t)`` runs linearly from ``r0`` down to
  ``taper_end_ror_c_per_min`` over ``taper_duration_seconds``, then holds at
  the end value. ``r0 = clamp(ror_engage, taper_end_ror_c_per_min,
  taper_start_max_ror_c_per_min)`` — anchored to what the roast MEASURED, not
  a value chosen ahead of time, capped at a configured start-max, and FLOORED
  at the end value so a degenerate low/negative engagement RoR (e.g. a
  post-charge-crash FC) cannot start the setpoint below where the taper
  finishes (that would over-cut on tick 1).
* **Never-add-heat-beyond-entry.** The output ceiling is
  ``effective_ceiling = max(1, min(heat_ceiling_percent, heat_engage))`` — the
  loop's output can never climb above the heat the roast held at the moment
  the loop engaged, which is exactly the failure mode the roast-9/10 A/B
  exposed (the loop must never *add* heat post-FC beyond what pre-FC control
  already committed to). The 1 % anti-stall floor wins over this clamp — a
  0 % heat-at-engagement handoff must not pin the whole DEVELOPMENT dwell at
  0 %. ``effective_floor = min(heat_floor_percent, effective_ceiling)``, so
  the box collapses downward together rather than going empty.

The controller is deliberately **stateful but deterministic**: all timing
(``dt``) is passed in by the caller rather than read from a clock, so the same
input sequence always produces the same output sequence — safe to replay and
to exercise in tests without a fake clock. The taper's own elapsed clock is
likewise caller-supplied (accumulated only across accepted-and-executed
actuations, mirroring the controller's ``_post_fc_last_actuation_monotonic``
discipline) rather than read from a wall clock here.

Bumpless transfer at the handoff is exact only when ``r0 == ror_engage`` (the
usual case — the measured engagement RoR already sits inside
``[taper_end_ror_c_per_min, taper_start_max_ror_c_per_min]``). When the
engagement RoR is degenerate and ``r0`` gets clamped away from it, tick-1's
zero-error assumption no longer holds and the loop takes a deliberate, gentle
first-tick correction toward the clamped setpoint instead — this is
documented here rather than claimed away, because a docstring asserting
"always bumpless" would be false in that edge case (the comment-that-lies
lesson).

**D96 bounded-bidirectional heat recovery (#559, ``PostFirstCrackControl.
recovery_enabled``, default ``False``).** Roast 15 (run ``8ac8a5e4``) showed
the never-add-heat-beyond-entry clamp binding exactly when the advisor uses
fan as intended: fan 30→90 crashed measured RoR 7→3 °C/min while heat sat
ceiling-locked at the 60 % entry value with ZERO raise authority — the bean
crawled 183→188 °C over 115 s and dropped 7 °C short. D96 relaxes the clamp
under a bounded RECOVERY law, layered entirely on top of the D88 taper
without touching it:

* **Entry.** When ``setpoint - ema`` (the SAME error :meth:`compute` already
  computes) exceeds ``recovery_trigger_margin_c_per_min`` for
  ``recovery_confirm_ticks`` consecutive :meth:`compute` calls, recovery
  ACTIVATES and the effective ceiling becomes ``min(heat_ceiling_percent,
  heat_engage_percent + recovery_headroom_percentage_points)`` — a hard,
  error-INDEPENDENT cap (never scaled by how far below setpoint RoR has
  fallen) — replacing D88's ``effective_ceiling`` for as long as recovery
  stays active. Entry is immediate once confirmed: the ceiling jumps to its
  recovery value in one step, because the failure this responds to (roast
  15) was itself a delay in getting any raise authority at all.
* **Exit.** Symmetric in shape but NOT in margin: recovery deactivates when
  ``setpoint - ema`` falls to or below ``recovery_exit_margin_c_per_min``
  (strictly smaller than the entry margin — a config validator enforces
  this) for ``recovery_confirm_ticks`` consecutive calls. The asymmetric gap
  between the two margins is a deliberate limit-cycle guard: an equal
  entry/exit threshold sitting at the deadband's own edge would let ordinary
  RoR noise cross back and forth and re-trigger entry immediately after an
  exit; the wider combined gap does not span on tick-to-tick noise alone
  (see the mandatory limit-cycle convergence test in
  ``tests/test_post_fc_control.py``).
* **Exit glide.** Once exit is confirmed, the effective ceiling does not
  snap back to ``heat_engage_percent`` — it descends by at most
  ``recovery_exit_glide_pp_per_tick`` per :meth:`compute` call until it
  reaches ``heat_engage_percent``, then locks there (byte-identical to D88
  from that point on). This bounds how hard a retreating ceiling can force
  heat down even if the PI's own output was pinned at the recovery ceiling
  the instant exit begins — entry is fast (time-critical: the failure this
  law fixes IS a delay), exit is deliberately slower (not time-critical, and
  the direct guard against a raise→recover→snap-cut→crash→re-trigger cycle).
  The glide state is derived PER TICK from ``(recovery active?, ticks since
  exit was confirmed)`` rather than carried as a separately-mutated
  descending value — nothing to get out of sync with the counters that
  already drive entry/exit.
* **Structural runaway-impossibility, preserved.** Recovery's ceiling is a
  hard cap that does not grow with repeated entry/exit cycles — re-entering
  recovery after an exit caps at the identical
  ``heat_engage_percent + recovery_headroom_percentage_points``, never a
  compounding value. Combined with the taper setpoint's own bound (it is
  measured-anchored and monotonically non-increasing toward
  ``taper_end_ror_c_per_min`` — it can never sit above a fixed floor
  forever the way D83's fixed 8.0 °C/min target did against roast 9/10's
  measured 6.1), the error that can ever drive the PI is bounded by a
  constant and the ceiling that error can ever unlock is bounded by a
  constant — roast 9's runaway required BOTH an unbounded-forever error AND
  an unbounded-upward ceiling, and this law removes both independently.
* **Precondition (config-enforced):** ``recovery_enabled=True`` REQUIRES
  ``PostFirstCrackControl.ceiling_guard_drop_enabled=True`` — see that
  model's cross-field validator and its corrected module docstring for why:
  a law that can raise heat above entry with no deterministic 196 °C anchor
  would leave the bitter line owned solely by the advisor's own judgment.
* **Fan is unaffected.** This law is heat-only; DEVELOPMENT's fan box stays
  the full 0-100 range the advisor already has today (D89/#498). The
  two-lever worst case (fan crashes RoR, heat recovers, temperature climbs
  faster) is bounded by the SAME hard, non-compounding heat cap regardless
  of how the fan is used or how many entry/exit cycles occur — the c8
  advisor teaching (a separate slice) is a judgment/efficiency layer on top
  of this structural bound, not a substitute for it.
* **Diagnostics (PR #560 Codex finding):** :attr:`PostFcControlOutput.
  recovery_active` alone cannot distinguish the ``GLIDING`` tail from
  ``HOLDING`` — it flips ``False`` the instant exit is confirmed even
  though the ceiling can still sit well above the D88 base for several more
  ticks. :class:`PostFcHeatAuthorityState` (``HOLDING``/``RECOVERING``/
  ``GLIDING``) is the authoritative three-way state; any caller reasoning
  about "is the ceiling still elevated above the D88 base right now" (e.g.
  the controller's guard-eligibility check, immediately below) reads
  :attr:`PostFcControlOutput.heat_authority_state` directly, never the
  derived boolean.
* **Guard-eligibility coupling (PR #560 Codex finding):** the CALLER
  (:meth:`~roastpilot_agent.controller.RoastController.
  _apply_deterministic_post_fc_levers`) skips this tick's heat/fan WRITE
  entirely — restoring the tentative :meth:`compute` step exactly like a
  rejected write — whenever this step's ``heat_authority_state`` is not
  ``HOLDING`` AND the SAME tick is eligible for the 196 °C ceiling-guard
  drop (``ceiling_guard_drop_enabled`` AND ``bean_temp_c >=
  ceiling_guard_temp_c``): without this, the raised/gliding heat command
  would still reach the roaster on the exact tick the guard also fires,
  because the lever write (this method) runs BEFORE the guard check in
  ``tick()``'s order. This module itself has no drop/guard concept
  (unchanged — see the module docstring above); the skip is entirely the
  caller's responsibility, scoped to RECOVERING/GLIDING ticks only so
  flag-off and recovery-inactive (``HOLDING``) behaviour stays byte-for-byte
  identical to before this fix.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from roastpilot_agent.config import PostFirstCrackControl
from roastpilot_agent.models import PostFcHeatAuthorityState

# Re-exported for existing callers/tests that import PostFcHeatAuthorityState
# from this module (D96 slice 2, #559): the enum's HOME moved to models.py
# (so AdvisorContext, in advisor.py, can carry it without models.py — a leaf
# module with no roastpilot_agent imports of its own — importing back into
# post_fc_control.py, which would cycle). Same reasoning models.py already
# documents for RoastPhase and DropReason.
__all__ = [
    "PostFcControlOutput",
    "PostFcControllerState",
    "PostFcHeatAuthorityState",
    "PostFcRorController",
]


@dataclass(frozen=True)
class PostFcControllerState:
    """An immutable snapshot of :class:`PostFcRorController`'s mutable state.

    Slice B2 (#405) needs this for the #412 told==enforced control-path rule
    applied to a *stateful* loop: the integrator/EMA/taper clock must advance
    only after an ACCEPTED safety-gated write (ALLOW/CLAMP), never on a REJECT
    (e.g. a rate-limited tick) or any other non-actuated path. The caller takes
    a snapshot with :meth:`PostFcRorController.snapshot_state` **before**
    calling :meth:`PostFcRorController.compute`, and — only when the resulting
    command was rejected — restores it with
    :meth:`PostFcRorController.restore_state` so the tentative step is fully
    undone, as if ``compute`` had never run. The controller's OWNER (Slice
    B2's controller wiring) additionally persists this alongside the rest of
    its snapshot/restore path (C2, D88) so the taper survives a same-process
    save/restore and clears on disengage — see
    :meth:`PostFcRorController.snapshot_state`'s docstring for that discipline.

    Immutable and side-effect free: holding one of these carries no reference
    to the controller and cannot itself mutate it.
    """

    integrator: float
    bias_percent: float
    ema: float | None
    #: The taper's elapsed time (seconds) since engagement, advanced only on
    #: the ACTUATION clock (D88 amendment C1) — see
    #: :meth:`PostFcRorController.compute`.
    taper_elapsed_seconds: float
    #: The taper's starting setpoint ``r0`` (°C/min), captured once at
    #: :meth:`PostFcRorController.reset` and constant for the engagement's
    #: lifetime (D88).
    taper_r0_c_per_min: float
    #: The heat percentage the roast held at the moment this engagement's
    #: :meth:`PostFcRorController.reset` was called; the basis for the
    #: never-add-heat-beyond-entry ``effective_ceiling`` (D88).
    heat_engage_percent: int
    #: D96 recovery entry counter: consecutive :meth:`compute` calls where
    #: the error has exceeded ``recovery_trigger_margin_c_per_min``. Resets
    #: to 0 the instant a tick's error does not exceed the margin — entry
    #: requires an UNBROKEN run of ``recovery_confirm_ticks`` ticks, never an
    #: accumulated total.
    recovery_ticks_above_trigger: int
    #: D96 recovery exit counter: consecutive :meth:`compute` calls, WHILE
    #: recovery is active, where the error has fallen to or below
    #: ``recovery_exit_margin_c_per_min``. Resets to 0 the instant a tick's
    #: error exceeds the exit margin. Irrelevant (and held at 0) whenever
    #: recovery is not active.
    recovery_ticks_within_exit: int
    #: Whether the D96 recovery ceiling is currently active (``True``) or
    #: this engagement is on the plain D88 never-add-heat-beyond-entry
    #: ceiling (``False``).
    recovery_active: bool
    #: Ticks elapsed since exit was CONFIRMED (i.e. since ``recovery_active``
    #: last flipped ``True`` -> ``False``), or ``None`` if recovery has never
    #: exited (or has never engaged) this engagement. Drives the exit glide:
    #: the effective ceiling has descended by
    #: ``min(recovery_exit_glide_pp_per_tick * this_value, recovery_headroom)``
    #: from the recovery ceiling toward ``heat_engage_percent`` — a pure
    #: function of this counter, never a separately-mutated descending value,
    #: so nothing can drift out of sync with it.
    recovery_ticks_since_exit: int | None


class PostFcControlOutput(BaseModel, frozen=True):
    """One post-FC PI control step's result, with full diagnostics.

    Carries not just the actuated heat command but the internal state the
    caller (Slice B2's controller wiring), the decision trace, and tests need
    to see inside the loop: the taper setpoint the error was computed against,
    the smoothed RoR, the resulting error, the integrator's value after this
    step, the effective (never-add-heat-beyond-entry) box this step clamped
    into, and whether the output was saturated (clamped) this step.

    All temperatures are Celsius; RoR is °C/min; ``heat_percent`` is a
    percentage in ``[effective_floor_percent, effective_ceiling_percent]``.
    """

    #: The commanded heat percentage for this control step, already clamped to
    #: this step's effective ``[effective_floor_percent,
    #: effective_ceiling_percent]`` box and rounded to the nearest integer
    #: (Hottop levers are integer percentages).
    heat_percent: int
    #: The taper's target RoR (°C/min) at this step's elapsed time — linear
    #: from ``r0`` down to ``taper_end_ror_c_per_min`` over
    #: ``taper_duration_seconds``, held at the end value after (D88).
    setpoint_c_per_min: float
    #: ``setpoint_c_per_min - smoothed_ror_c_per_min`` for this step (before
    #: the deadband is applied to decide integration) — positive means RoR is
    #: too LOW (more heat needed), negative means too HIGH (less heat needed).
    error_c_per_min: float
    #: The EMA-smoothed RoR this step's error was computed from.
    smoothed_ror_c_per_min: float
    #: The integrator's accumulated value *after* this step (post anti-windup
    #: rollback), in the same units the loop scales by ``ki`` — i.e. accumulated
    #: (°C/min)·seconds of effective error.
    integrator: float
    #: This step's effective heat ceiling: D88's never-add-heat-beyond-entry
    #: value, ``max(1, min(heat_ceiling_percent, heat_engage_percent))``,
    #: UNLESS D96 recovery is active (or gliding back down from it) this
    #: step, in which case it is the recovery ceiling (possibly still
    #: descending toward the D88 value — see :attr:`recovery_active` and
    #: ``PostFcControllerState.recovery_ticks_since_exit``). Exposed for
    #: tests/diagnostics — the loop's output can never exceed this value.
    effective_ceiling_percent: int
    #: This step's effective floor: ``min(heat_floor_percent,
    #: effective_ceiling_percent)`` (D88) — the box collapses downward, never
    #: empty, when a low ``heat_engage_percent`` pulls the ceiling below the
    #: configured static floor.
    effective_floor_percent: int
    #: Whether the unclamped PI output fell outside this step's effective heat
    #: box (the anti-windup rollback engaged when combined with continued
    #: integration in the saturating direction).
    saturated: bool
    #: D96 (#559): this step's heat-authority regime — ``HOLDING`` (the
    #: plain D88 ceiling), ``RECOVERING`` (the D96 ceiling fully raised), or
    #: ``GLIDING`` (recovery has exited but the ceiling has not yet fully
    #: descended back to the D88 base). Always ``HOLDING`` when
    #: ``PostFirstCrackControl.recovery_enabled`` is ``False`` (the default),
    #: so a flag-off engagement's diagnostics are unaffected. This is the
    #: SAME state :meth:`compute` used internally to pick
    #: ``effective_ceiling_percent`` this step — any caller reasoning about
    #: "is the ceiling still elevated above the D88 base" (e.g.
    #: ``controller.py``'s guard-eligibility check, PR #560) MUST read this
    #: field, never :attr:`recovery_active` alone — that boolean cannot
    #: distinguish ``GLIDING`` from ``HOLDING`` (a Codex finding: it flips
    #: ``False`` the instant exit is confirmed even though the ceiling can
    #: still sit well above the D88 base for several more ticks).
    heat_authority_state: PostFcHeatAuthorityState
    #: **Derived, kept for the callers this slice already shipped.**
    #: ``heat_authority_state is not PostFcHeatAuthorityState.HOLDING`` —
    #: ``True`` for BOTH ``RECOVERING`` and ``GLIDING``. This is NOT the raw
    #: internal ``recovery_active`` flag :meth:`compute` tracks (which is
    #: ``False`` during ``GLIDING``) — it is the OR of the two non-holding
    #: states, so existing "is anything elevated right now" callers stay
    #: correct through the fix for the diagnostics gap above. New code should
    #: prefer :attr:`heat_authority_state` directly when the HOLDING/
    #: RECOVERING/GLIDING distinction matters.
    recovery_active: bool


class PostFcRorController:
    """A pure, stateful PI controller holding a post-FC declining RoR taper (D88).

    Holds the mutable loop state (the integrator, the EMA RoR estimate, and the
    taper clock/setpoint) across successive :meth:`compute` calls.
    Deterministic and side-effect free: every call is a pure function of
    ``self``'s current state plus the arguments passed in — no wall-clock
    reads, no I/O, no randomness — so a recorded sequence of
    ``(measured_ror_c_per_min, dt_seconds)`` pairs always replays to the exact
    same output sequence.

    **This controller does not write to a roaster.** It only computes a heat
    percentage; the caller (Slice B2) is responsible for routing that value
    through :mod:`roastpilot_agent.safety`'s gate before any MCP write, exactly
    like every other roaster write in this codebase (the every-write-through-
    safety-policy invariant).

    Construct one instance per roast run (or per post-FC engagement) and call
    :meth:`reset` once at the bumpless-handoff point (first-crack detection),
    then :meth:`compute` once per control tick thereafter.
    """

    def __init__(self, config: PostFirstCrackControl) -> None:
        """Construct the controller from its configuration.

        Args:
            config: The PI gains, taper parameters, and heat/fan bounds (D88).
                The controller holds a reference to this config but never
                mutates it — all mutable state lives on ``self``.
        """
        self._config = config
        self._integrator: float = 0.0
        self._bias_percent: float = 0.0
        self._ema: float | None = None
        self._taper_elapsed_seconds: float = 0.0
        self._taper_r0_c_per_min: float = config.taper_end_ror_c_per_min
        self._heat_engage_percent: int = 0
        # D96 (#559) recovery state — all inert (recovery never activates)
        # while ``config.recovery_enabled`` is False, the default.
        self._recovery_ticks_above_trigger: int = 0
        self._recovery_ticks_within_exit: int = 0
        self._recovery_active: bool = False
        self._recovery_ticks_since_exit: int | None = None

    def reset(self, *, initial_heat_percent: int, ror_at_engagement_c_per_min: float) -> None:
        """Seed the loop for a bumpless handoff at first-crack engagement.

        Captures the two D88 engagement anchors — ``heat_engage_percent`` (this
        method's ``initial_heat_percent``) and ``ror_at_engagement_c_per_min``
        — resets the taper clock to zero, and computes the taper's starting
        setpoint ``r0``. Also clears the RoR EMA (the next :meth:`compute`
        call treats its first sample as the initial estimate, with no stale
        pre-FC RoR history) and seeds the loop's internal state so that a
        **zero-error** first :meth:`compute` call returns
        ``initial_heat_percent`` — i.e. the loop picks up exactly where the
        pre-FC heat level left off, with no heat dip or jump at the moment the
        loop takes over, PROVIDED ``r0 == ror_at_engagement_c_per_min`` (the
        usual case). This is bumpless transfer: without it, the loop would
        start from an empty integrator (implying 0 % heat) and the
        proportional term alone would have to make up the entire gap,
        producing a visible heat step at the exact tick the loop engages.

        **``r0`` — the taper's starting setpoint (D88):**
        ``r0 = clamp(ror_at_engagement_c_per_min, taper_end_ror_c_per_min,
        taper_start_max_ror_c_per_min)`` — anchored to what the roast actually
        MEASURED at the handoff instant, capped at the configured start-max,
        and FLOORED at the end value. The floor matters: a degenerate low or
        negative engagement RoR (e.g. a post-charge-crash FC, where RoR can
        read negative) would otherwise seed a setpoint BELOW the taper's own
        end value — the loop would then read a spurious "RoR too high" error
        and cut heat hard on tick 1, exactly the over-cut this floor exists to
        prevent. **When ``r0`` is clamped away from the measured RoR, tick-1's
        bumpless assumption above does not hold** — ``error_eff`` is nonzero
        from the first :meth:`compute` call, and the loop takes a deliberate,
        gentle first correction toward ``r0`` instead of reproducing
        ``initial_heat_percent`` exactly. This is intentional (a docstring
        claiming unconditional bumpless transfer here would be false).

        Bumpless-transfer math (the ``r0 == ror_at_engagement_c_per_min``
        case): at zero error (post-deadband, ``error_eff == 0``)
        :meth:`compute`'s output formula is ``ki * integrator + bias`` (the
        proportional term is 0 when ``error_eff`` is 0). This method seeds:

        * ``integrator = initial_heat_percent / ki`` when ``ki > 0`` (clamped
          to ``[0, effective_ceiling / ki]``, where ``effective_ceiling =
          max(1, min(heat_ceiling_percent, initial_heat_percent))`` — the same
          never-add-heat-beyond-entry range the running integrator is bounded
          to by :meth:`compute`'s anti-windup, so a handoff heat outside the
          configured box cannot seed a value the loop could never reach
          unclamped anyway), with ``bias = 0``.
        * ``integrator = 0`` and ``bias = initial_heat_percent`` when
          ``ki == 0`` — **the degenerate case**: with no integral gain, no
          scalar multiple of the integrator can hold a nonzero output at zero
          error, so the held level is carried directly as an explicit
          constant bias instead. A ``ki == 0`` configuration is then a pure-P
          loop *around* that fixed bias (``heat = bias + kp * error_eff``,
          clamped) rather than a true PI loop — construction with ``ki=0``
          never raises or divides by zero, but callers should not expect
          steady-state error elimination from a bias-only P loop (that is a
          known property of P-only control, not a defect in this seeding).

        Args:
            initial_heat_percent: The heat percentage the pre-FC controller
                (or the prior control state) was ACTUATING at the moment this
                loop takes over. Seeds the handoff bias AND becomes
                ``heat_engage_percent`` — the never-add-heat-beyond-entry
                basis for every subsequent :meth:`compute` call's effective
                ceiling this engagement (D88). Not otherwise validated against
                the config's box (a value outside the box is clamped the same
                way any other output is clamped, on the very first
                :meth:`compute` call).
            ror_at_engagement_c_per_min: The EMA-smoothed bean RoR (°C/min)
                the roast measured at this same handoff instant. Anchors the
                taper's starting setpoint ``r0`` (see above) — never a fixed
                value chosen ahead of time (D83's superseded law).
        """
        self._ema = None
        self._taper_elapsed_seconds = 0.0
        self._heat_engage_percent = initial_heat_percent
        # D96 (#559): a fresh engagement always starts on the plain D88
        # ceiling — a carried-over recovery state from a PRIOR engagement
        # must never leak into this one (mirrors the taper clock reset
        # above). Zeroed BEFORE `_effective_ceiling_percent()` is read below,
        # so the bumpless seed always sees the D88 (non-recovery) ceiling.
        self._recovery_ticks_above_trigger = 0
        self._recovery_ticks_within_exit = 0
        self._recovery_active = False
        self._recovery_ticks_since_exit = None
        config = self._config
        self._taper_r0_c_per_min = max(
            config.taper_end_ror_c_per_min,
            min(ror_at_engagement_c_per_min, config.taper_start_max_ror_c_per_min),
        )
        ki = config.ki_percent_per_ror_second
        effective_ceiling = self._effective_ceiling_percent()
        if ki > 0:
            # Bound the seed the same way `compute`'s anti-windup bounds the
            # running integrator: never above what would drive the unclamped
            # output past THIS ENGAGEMENT's effective ceiling (D88's
            # never-add-heat-beyond-entry clamp, not the static config
            # ceiling), never below 0 (a negative integrator would imply a
            # negative heat bias, which the floor already guarantees is
            # unreachable in practice since heat_floor >= 1).
            seeded = initial_heat_percent / ki
            self._integrator = max(0.0, min(seeded, effective_ceiling / ki))
            self._bias_percent = 0.0
        else:
            # Degenerate ki==0 path (see docstring): there is no integrator
            # scaling that can hold a bias through the `ki * integrator` term,
            # so the handoff level is carried as an explicit constant instead.
            self._integrator = 0.0
            self._bias_percent = float(initial_heat_percent)

    def _never_add_heat_ceiling_percent(self) -> int:
        """This engagement's D88 never-add-heat-beyond-entry ceiling.

        ``max(1, min(heat_ceiling_percent, heat_engage_percent))`` — the loop
        can never command more heat than the roast held at the instant it
        engaged, but the 1 % anti-stall floor wins over that clamp: a 0 %
        heat-at-engagement handoff must not pin the whole DEVELOPMENT dwell at
        0 % (a stall the loop then could never climb out of). This is the
        BASE ceiling D96 recovery (below) raises above, and glides back down
        to, when active.

        Returns:
            The D88 ceiling in whole percent.
        """
        return max(1, min(self._config.heat_ceiling_percent, self._heat_engage_percent))

    def _recovery_ceiling_percent(self) -> int:
        """The D96 recovery ceiling: the hard, error-independent raise cap.

        ``min(heat_ceiling_percent, heat_engage_percent +
        recovery_headroom_percentage_points)`` — never scaled by how far
        below setpoint RoR has fallen, and never above the static
        ``heat_ceiling_percent`` outer bound regardless of how low
        ``heat_engage_percent`` was.

        Returns:
            The recovery ceiling in whole percent.
        """
        config = self._config
        return min(
            config.heat_ceiling_percent,
            self._heat_engage_percent + config.recovery_headroom_percentage_points,
        )

    def _effective_ceiling_percent(self) -> int:
        """This step's effective heat ceiling — D88's base value, or the D96
        recovery ceiling (possibly gliding back down from it) when relevant.

        Three cases, in order:

        1. **Recovery not active and never has exited this engagement**
           (``self._recovery_active`` is ``False`` and
           ``self._recovery_ticks_since_exit`` is ``None``): plain D88 —
           returns :meth:`_never_add_heat_ceiling_percent`.
        2. **Recovery currently active** (``self._recovery_active`` is
           ``True``): returns :meth:`_recovery_ceiling_percent` — the raise
           is immediate and full the instant entry is confirmed (D96: entry
           is time-critical, unlike exit).
        3. **Gliding back down** (recovery was active, has since exited,
           ``self._recovery_ticks_since_exit`` is a non-``None`` count of
           ticks since that exit was confirmed): the ceiling has descended by
           ``recovery_exit_glide_pp_per_tick * recovery_ticks_since_exit``
           from the recovery ceiling, floored at the D88 base value — a pure
           function of the tick counter alone (D96: nothing to fall out of
           sync with it, unlike a separately-mutated descending value).

        Returns:
            The effective ceiling in whole percent.
        """
        base = self._never_add_heat_ceiling_percent()
        if self._recovery_active:
            return max(base, self._recovery_ceiling_percent())
        if self._recovery_ticks_since_exit is not None:
            config = self._config
            glided_down = self._recovery_ceiling_percent() - (
                config.recovery_exit_glide_pp_per_tick * self._recovery_ticks_since_exit
            )
            return max(base, glided_down)
        return base

    def _effective_floor_percent(self) -> int:
        """This engagement's effective floor (D88).

        ``min(heat_floor_percent, effective_ceiling_percent)`` — the box
        collapses DOWNWARD together with a lowered ceiling rather than ever
        going empty (floor above ceiling).

        Returns:
            The effective floor in whole percent.
        """
        return min(self._config.heat_floor_percent, self._effective_ceiling_percent())

    def _taper_setpoint_c_per_min(self) -> float:
        """This step's taper target RoR (°C/min), per the elapsed taper clock.

        Linear from ``r0`` (captured at :meth:`reset`) down to
        ``taper_end_ror_c_per_min`` over ``taper_duration_seconds``; held at
        the end value once the duration has fully elapsed (D88).

        Returns:
            The current setpoint in °C/min.
        """
        config = self._config
        duration = config.taper_duration_seconds
        progress = min(1.0, self._taper_elapsed_seconds / duration)
        return self._taper_r0_c_per_min + progress * (
            config.taper_end_ror_c_per_min - self._taper_r0_c_per_min
        )

    def snapshot_state(self) -> PostFcControllerState:
        """Capture the loop's mutable state before a tentative :meth:`compute` step.

        Slice B2 (#405) calls this immediately before ``compute`` so a REJECTed
        (e.g. rate-limited) safety verdict can undo the tentative step with
        :meth:`restore_state` — the loop's integrator/EMA/taper clock must
        advance only on an ACCEPTED (ALLOW/CLAMP) write, mirroring the pre-FC
        adaptive-trim "advance state only on accepted write" rule (#412). The
        SAME snapshot doubles as the controller's persisted state (D88
        amendment C2): the caller's snapshot/restore path (mirroring the
        pre-FC damping state) carries ``taper_r0_c_per_min``,
        ``taper_elapsed_seconds``, and ``heat_engage_percent`` across a
        same-process save/restore, and clears them (by never restoring a
        stale snapshot) the moment ``_post_fc_engaged`` goes False — a fresh
        engagement always starts from a fresh :meth:`reset` call, never a
        carried-over snapshot from a prior engagement.

        Returns:
            An immutable :class:`PostFcControllerState` capturing the current
            integrator, bias, EMA, and taper state.
        """
        return PostFcControllerState(
            integrator=self._integrator,
            bias_percent=self._bias_percent,
            ema=self._ema,
            taper_elapsed_seconds=self._taper_elapsed_seconds,
            taper_r0_c_per_min=self._taper_r0_c_per_min,
            heat_engage_percent=self._heat_engage_percent,
            recovery_ticks_above_trigger=self._recovery_ticks_above_trigger,
            recovery_ticks_within_exit=self._recovery_ticks_within_exit,
            recovery_active=self._recovery_active,
            recovery_ticks_since_exit=self._recovery_ticks_since_exit,
        )

    def restore_state(self, state: PostFcControllerState) -> None:
        """Restore a previously captured state, undoing a tentative ``compute`` step.

        Args:
            state: A snapshot from :meth:`snapshot_state`, taken before the
                :meth:`compute` call being undone.
        """
        self._integrator = state.integrator
        self._bias_percent = state.bias_percent
        self._ema = state.ema
        self._taper_elapsed_seconds = state.taper_elapsed_seconds
        self._taper_r0_c_per_min = state.taper_r0_c_per_min
        self._heat_engage_percent = state.heat_engage_percent
        self._recovery_ticks_above_trigger = state.recovery_ticks_above_trigger
        self._recovery_ticks_within_exit = state.recovery_ticks_within_exit
        self._recovery_active = state.recovery_active
        self._recovery_ticks_since_exit = state.recovery_ticks_since_exit

    def compute(self, *, measured_ror_c_per_min: float, dt_seconds: float) -> PostFcControlOutput:
        """Run one PI control step and return the commanded heat + diagnostics.

        Steps (D88):

        1. **Advance the taper clock** by ``dt_seconds`` **clamped to at most
           one ``control_interval_seconds``** (see the ``dt_seconds`` argument
           below — D88 amendment C1: the caller passes only ACTUATION time, so
           a paused/HOLD/rate-limited stretch does not march the taper's
           setpoint down; the clamp additionally guards a RESUME after a
           GAP — a skipped-RoR outage lasting several cadence intervals must
           not be swallowed into a single oversized taper-clock jump on the
           first tick after it, which would march the setpoint down by the
           whole outage in one step rather than by the (at most) one interval
           the DEVELOPMENT tick loop can actually observe) and read this
           step's taper setpoint (:meth:`_taper_setpoint_c_per_min`): linear
           from ``r0`` down to ``taper_end_ror_c_per_min`` over
           ``taper_duration_seconds``, held at the end value after.
        2. **EMA-smooth** the RoR sample: ``ema = alpha*measured +
           (1-alpha)*prev_ema``, or ``ema = measured`` on the very first call
           (no prior estimate to blend with).
        3. **Error:** ``error = setpoint - ema``. RoR too LOW (below the
           taper's current setpoint) gives a positive error, which — per the
           sign convention this loop uses — commands MORE heat; RoR too HIGH
           gives a negative error and commands LESS heat.
        4. **Deadband:** if ``abs(error) <= ror_deadband_c_per_min`` the loop
           HOLDS — ``error_eff`` is forced to 0.0 and the integrator does NOT
           accumulate this step (the #386/#412 lesson: a deadband that only
           gates the proportional term but keeps integrating would still
           slowly drift the output inside the band).
        5. Otherwise ``error_eff = error`` and the integrator tentatively
           accumulates ``error_eff * <the SAME clamped dt>`` from step 1 — the
           same gap-resume exposure applies here: an uncapped ``dt_seconds``
           after a long outage would inject a single oversized integration
           step even on a NON-saturating tick (anti-windup only bounds the
           integrator while the output is actively saturating in the error's
           direction; it does not protect a normal in-box step), so the
           integrator uses the identical clamped value, not the raw
           ``dt_seconds``.
        6. **Output:** ``bias + ki * integrator + kp * error_eff`` (unclamped).
           For a ``ki > 0`` loop the handoff bias lives entirely in the
           integrator seeded by :meth:`reset` and ``bias`` is 0, so this
           reduces to ``ki * integrator + kp * error_eff`` — at exactly zero
           error (post-deadband) the output is ``ki * integrator``, the held
           level. For the ``ki == 0`` degenerate case (see :meth:`reset`),
           ``bias`` alone carries the handoff level and the loop is pure-P
           around it.
        7. **Clamp** to this engagement's effective box,
           ``[effective_floor_percent, effective_ceiling_percent]`` (D88's
           never-add-heat-beyond-entry clamp — see :meth:`_effective_ceiling_percent`
           / :meth:`_effective_floor_percent`), which is ALWAYS within (never
           wider than) the static ``[heat_floor_percent, heat_ceiling_percent]``
           config box; ``saturated`` is set when the unclamped output fell
           outside that effective range.
        8. **Anti-windup (conditional integration):** if the unclamped output
           saturated AND the tentative integration step pushed further toward
           that same rail (i.e. integrating made the saturation worse, not
           better), the integrator's tentative update is rolled back to its
           pre-step value. This keeps the integrator bounded during a
           sustained saturating error (it does not run away) and lets the loop
           leave the rail immediately once the error direction reverses —
           there is no accumulated backlog to unwind first.

        Args:
            measured_ror_c_per_min: The latest bean rate-of-rise sample
                (°C/min), already computed by the caller from the raw
                temperature curve. This is the *raw* (pre-EMA) sample; the EMA
                smoothing happens inside this method.
            dt_seconds: The elapsed seconds since the previous call to
                :meth:`compute` (or since :meth:`reset`, for the first call).
                Passed in by the caller rather than read from a clock, so this
                method stays deterministic and replay-safe. **Must be the
                ACTUATION clock, not the wall clock** (D88 amendment C1): the
                caller (Slice B2) supplies elapsed time since the last
                ACCEPTED-AND-EXECUTED write, exactly the same discipline as
                ``_post_fc_last_actuation_monotonic`` already applies to the
                cadence timer — this method advances the taper's own elapsed
                clock by this same value, so a REJECTed/rate-limited/paused
                stretch (which never calls ``compute`` in the first place, or
                calls it but has the tentative step undone by
                :meth:`restore_state`) cannot silently march the setpoint
                down. **Internally clamped to at most one
                ``control_interval_seconds`` per call** (gap-resume fix,
                Codex finding on the #405 PR): the controller's own
                fail-closed guard skips a tick's actuation entirely when RoR
                is unavailable that tick (``bean_ror_c_per_min is None``)
                WITHOUT advancing ``_post_fc_last_actuation_monotonic`` — so
                after an N-second RoR outage, the first tick with a RoR
                sample again would otherwise present a single ``dt_seconds``
                spanning the WHOLE outage, jumping the taper clock (and the
                integrator's accumulated step) by N seconds in one call
                instead of by the at most one interval a resuming
                DEVELOPMENT tick loop can actually observe — exactly the
                "paused stretch silently marches the setpoint" failure C1
                exists to prevent, just entered through a gap-resume rather
                than a rejected write. The RAW ``dt_seconds`` must still be
                strictly positive: a zero or negative ``dt`` would either
                freeze or reverse the integrator's accumulated direction,
                which is never a valid tick duration — the caller is
                responsible for supplying a sane value (e.g. the configured
                ``control_interval_seconds`` on the very first post-handoff
                tick). The clamp is a pure no-op under normal cadence (where
                ``dt_seconds`` already approximately equals
                ``control_interval_seconds``).

        Returns:
            The :class:`PostFcControlOutput` for this step: the clamped
            integer heat percentage plus the taper setpoint, the smoothed RoR,
            the error, the post-step integrator value, this step's effective
            box, and whether this step saturated.

        Raises:
            ValueError: If ``dt_seconds`` is not strictly positive.
        """
        if dt_seconds <= 0.0:
            raise ValueError(f"dt_seconds must be > 0 ({dt_seconds})")
        config = self._config
        # Gap-resume fix (Codex finding, #405 PR): clamp the dt this step
        # actually ADVANCES STATE BY to at most one control_interval_seconds.
        # The controller skips a tick's actuation entirely (never calls
        # compute at all) whenever RoR is unavailable that tick, WITHOUT
        # advancing `_post_fc_last_actuation_monotonic` — so after an
        # N-second RoR outage, the next successful compute call would
        # otherwise receive a `dt_seconds` spanning the WHOLE outage, and
        # swallow it into a single oversized step. Capping here means a gap
        # advances the taper (and the integrator, below) by at most one
        # interval's worth, matching what a normally-cadenced tick loop can
        # actually observe — a pure no-op under normal cadence, where
        # dt_seconds already approximately equals control_interval_seconds.
        effective_dt = min(dt_seconds, config.control_interval_seconds)
        # D88 amendment C1: advance the taper's own clock by the SAME
        # actuation-only (and now gap-capped) dt the integrator uses — see
        # the docstring above. This is tentative like every other mutation in
        # this method: a subsequent `restore_state` (rejected/failed write)
        # undoes it too.
        self._taper_elapsed_seconds += effective_dt
        setpoint = self._taper_setpoint_c_per_min()

        alpha = config.ror_smoothing_alpha
        ema = (
            measured_ror_c_per_min
            if self._ema is None
            else alpha * measured_ror_c_per_min + (1.0 - alpha) * self._ema
        )
        self._ema = ema

        error = setpoint - ema
        within_deadband = abs(error) <= config.ror_deadband_c_per_min
        error_eff = 0.0 if within_deadband else error

        # D96 (#559): advance the recovery entry/exit state machine from the
        # SAME `error` this step already computed (setpoint - ema, BEFORE the
        # deadband — recovery reasons about the raw shortfall, not the
        # deadband-gated value the PI's own P/I terms use), then read the
        # ceiling/floor below. Inert (recovery never activates) whenever
        # `config.recovery_enabled` is False, the default.
        self._advance_recovery_state(error)

        pre_step_integrator = self._integrator
        # Same gap-cap applies to the integrator's accumulation: anti-windup
        # (below) only bounds the integrator while the output is ACTIVELY
        # saturating in the error's direction — it does not protect a normal,
        # non-saturating in-box step, so an uncapped dt_seconds after a long
        # outage would still inject a single oversized integration step here
        # (verified empirically: a 60s gap-swallowed tick moved heat from a
        # 72% bumpless hold down to 60% in one step, vs. a negligible move on
        # a normal 5s cadence tick with the same inputs).
        tentative_integrator = pre_step_integrator + error_eff * effective_dt

        unclamped = (
            self._bias_percent
            + config.ki_percent_per_ror_second * tentative_integrator
            + config.kp_percent_per_ror * error_eff
        )
        floor = self._effective_floor_percent()
        ceiling = self._effective_ceiling_percent()
        saturated = unclamped < floor or unclamped > ceiling

        # Anti-windup (conditional integration): roll the integrator back to
        # its pre-step value ONLY when this step's integration pushed the
        # output FURTHER into the rail it saturated (integrating made the
        # saturation worse). Every other case KEEPS the tentative update:
        #   - not saturated, or a deadband hold (`error_eff == 0`) — there is
        #     nothing to undo; and
        #   - saturated but still climbing TOWARD the box: pinned at the floor
        #     with a positive error (`ki*integrator + kp*error` has not yet
        #     reached the floor) or pinned at the ceiling with a negative
        #     error. Keep integrating so the loop keeps moving toward target;
        #     the same-rail rollback still bounds the integrator once
        #     `ki*integrator` reaches the far rail.
        #
        # The "saturated but climbing toward the box" case is REACHABLE in
        # normal wide-box post-FC transients (an Opus safety review disproved
        # the earlier "unreachable" claim by fuzzing) — it is not a
        # negative-gain-only edge, so it is covered by an explicit test.
        pushing_further = saturated and (
            (unclamped > ceiling and error_eff > 0.0) or (unclamped < floor and error_eff < 0.0)
        )
        self._integrator = pre_step_integrator if pushing_further else tentative_integrator

        clamped = max(float(floor), min(float(ceiling), unclamped))
        heat_authority_state = self._heat_authority_state()
        return PostFcControlOutput(
            heat_percent=round(clamped),
            setpoint_c_per_min=setpoint,
            error_c_per_min=error,
            smoothed_ror_c_per_min=ema,
            integrator=self._integrator,
            effective_ceiling_percent=ceiling,
            effective_floor_percent=floor,
            saturated=saturated,
            heat_authority_state=heat_authority_state,
            recovery_active=heat_authority_state is not PostFcHeatAuthorityState.HOLDING,
        )

    def _heat_authority_state(self) -> PostFcHeatAuthorityState:
        """This step's D96 heat-authority regime (PR #560 Codex findings,
        rounds 1 and 3).

        ``RECOVERING`` while ``self._recovery_active`` is ``True`` AND the
        ceiling this instant is ACTUALLY above the D88 base; ``GLIDING``
        while recovery has exited but ``self._recovery_ticks_since_exit`` is
        still counting AND the ceiling is still actually above the base;
        ``HOLDING`` in every other case (never engaged, fully settled, OR —
        round 3's finding — the internal counters say "active"/"gliding" but
        the ceiling never actually moved above the base to begin with).

        **Round 3 fix: reports ACTUAL elevation, not just internal
        entry/exit-counter state.** With zero headroom
        (``recovery_headroom_percentage_points=0``) or an engagement heat
        already AT ``heat_ceiling_percent``,
        :meth:`_recovery_ceiling_percent` equals
        :meth:`_never_add_heat_ceiling_percent` exactly — the entry/exit
        counters can still confirm (``self._recovery_active`` flips
        ``True``), but the ceiling itself never actually rises. Without this
        check, that no-op "recovery" would still report ``RECOVERING``,
        which the controller's guard/drop-eligibility skip
        (``_apply_deterministic_post_fc_levers``, PR #560 rounds 1/2) reads
        as "elevated" and would suppress a drop-tick write for NOTHING — no
        actual raise ever happened to justify the suppression. Basing the
        reported STATE on real elevation (rather than adding a second check
        at the controller call site) keeps told==enforced simplest: the
        controller already trusts this one field completely; teaching it a
        second "but only if ALSO really elevated" clause there would
        duplicate arithmetic this method already owns.

        Returns:
            The current heat-authority state.
        """
        base = self._never_add_heat_ceiling_percent()
        if self._recovery_active:
            return (
                PostFcHeatAuthorityState.RECOVERING
                if self._recovery_ceiling_percent() > base
                else PostFcHeatAuthorityState.HOLDING
            )
        if self._recovery_ticks_since_exit is not None:
            return (
                PostFcHeatAuthorityState.GLIDING
                if self._effective_ceiling_percent() > base
                else PostFcHeatAuthorityState.HOLDING
            )
        return PostFcHeatAuthorityState.HOLDING

    def _advance_recovery_state(self, error_c_per_min: float) -> None:
        """Advance the D96 (#559) recovery entry/exit counters for one tick.

        Inert (every counter stays 0, ``recovery_active`` stays ``False``)
        whenever ``PostFirstCrackControl.recovery_enabled`` is ``False`` (the
        default) — this method still runs every tick regardless of the flag,
        but it can never SET ``recovery_active`` True while the flag is off,
        so a flag-off engagement's ceiling is always exactly the D88 value.

        **Entry** (only reachable while NOT already active): counts
        consecutive ticks where ``error_c_per_min`` exceeds
        ``recovery_trigger_margin_c_per_min`` (RoR persistently BELOW the
        taper setpoint — a POSITIVE error under this loop's sign convention).
        Resets to 0 the instant a tick does not exceed the margin — entry
        requires an UNBROKEN run of ``recovery_confirm_ticks`` ticks. Once
        confirmed, ``recovery_active`` flips ``True`` immediately (no glide
        on entry — D96: the failure this responds to, roast 15, was itself a
        delay in getting any raise authority) and the exit counter/tick-since-
        exit state both reset (a fresh entry is not still "gliding" from a
        prior exit).

        **Exit** (only reachable while active): counts consecutive ticks
        where ``error_c_per_min`` has fallen to or below
        ``recovery_exit_margin_c_per_min`` (strictly smaller than the entry
        margin — a config validator enforces this asymmetry, the limit-cycle
        guard). Resets to 0 the instant a tick's error exceeds the exit
        margin. Once confirmed, ``recovery_active`` flips ``False`` and
        ``recovery_ticks_since_exit`` starts counting from 0 (immediately
        incremented to 1 THIS tick — see below) so
        :meth:`_effective_ceiling_percent`'s glide begins descending on the
        very next ceiling read, not one tick later.

        **While gliding** (``recovery_active`` is ``False`` and
        ``recovery_ticks_since_exit`` is not ``None``): this method still
        increments the counter by 1 every tick regardless of
        ``error_c_per_min`` — the glide is a pure function of elapsed ticks
        since exit, not of the RoR error, so it cannot itself be re-triggered
        by a noisy sample mid-glide. A fresh entry (if the error climbs back
        above the trigger margin again while gliding) clears the glide state
        immediately, per the entry branch above.

        Args:
            error_c_per_min: This tick's ``setpoint - ema`` (before the PI
                deadband is applied) — positive means RoR is below setpoint.
        """
        config = self._config
        if not config.recovery_enabled:
            return
        if self._recovery_active:
            # Active: only the EXIT counter advances.
            if error_c_per_min <= config.recovery_exit_margin_c_per_min:
                self._recovery_ticks_within_exit += 1
            else:
                self._recovery_ticks_within_exit = 0
            if self._recovery_ticks_within_exit >= config.recovery_confirm_ticks:
                self._recovery_active = False
                self._recovery_ticks_within_exit = 0
                self._recovery_ticks_above_trigger = 0
                self._recovery_ticks_since_exit = 1
        elif self._recovery_ticks_since_exit is not None:
            # Gliding down from a prior exit: the entry condition can still
            # re-trigger (re-checked below); otherwise the glide simply
            # advances by one tick.
            if error_c_per_min > config.recovery_trigger_margin_c_per_min:
                self._recovery_ticks_above_trigger += 1
            else:
                self._recovery_ticks_above_trigger = 0
            if self._recovery_ticks_above_trigger >= config.recovery_confirm_ticks:
                self._recovery_active = True
                self._recovery_ticks_above_trigger = 0
                self._recovery_ticks_within_exit = 0
                self._recovery_ticks_since_exit = None
            else:
                self._recovery_ticks_since_exit += 1
                # Settle back to HOLDING the instant the glide arithmetic
                # itself reaches the D88 base (PR #560 Codex finding's
                # corollary): without this, `_recovery_ticks_since_exit`
                # would keep counting forever after the ceiling has already
                # numerically bottomed out, permanently reporting GLIDING
                # even once nothing is actually elevated any more. This
                # mirrors `_effective_ceiling_percent`'s own
                # `max(base, glided_down)` floor — once the glide's raw
                # arithmetic value would fall AT or below that floor, there
                # is nothing left to glide down from.
                config_headroom = config.recovery_headroom_percentage_points
                recovery_ceiling = min(
                    config.heat_ceiling_percent,
                    self._heat_engage_percent + config_headroom,
                )
                glided_down = recovery_ceiling - (
                    config.recovery_exit_glide_pp_per_tick * self._recovery_ticks_since_exit
                )
                if glided_down <= self._never_add_heat_ceiling_percent():
                    self._recovery_ticks_since_exit = None
        else:
            # Never engaged (or fully settled back to the D88 base ceiling)
            # this engagement: only the ENTRY counter advances.
            if error_c_per_min > config.recovery_trigger_margin_c_per_min:
                self._recovery_ticks_above_trigger += 1
            else:
                self._recovery_ticks_above_trigger = 0
            if self._recovery_ticks_above_trigger >= config.recovery_confirm_ticks:
                self._recovery_active = True
                self._recovery_ticks_above_trigger = 0
