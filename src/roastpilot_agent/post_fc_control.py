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
"""

from dataclasses import dataclass

from pydantic import BaseModel

from roastpilot_agent.config import PostFirstCrackControl


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
    #: This step's never-add-heat-beyond-entry ceiling: ``max(1,
    #: min(heat_ceiling_percent, heat_engage_percent))`` (D88). Exposed for
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

    def _effective_ceiling_percent(self) -> int:
        """This engagement's never-add-heat-beyond-entry ceiling (D88).

        ``max(1, min(heat_ceiling_percent, heat_engage_percent))`` — the loop
        can never command more heat than the roast held at the instant it
        engaged, but the 1 % anti-stall floor wins over that clamp: a 0 %
        heat-at-engagement handoff must not pin the whole DEVELOPMENT dwell at
        0 % (a stall the loop then could never climb out of).

        Returns:
            The effective ceiling in whole percent.
        """
        return max(1, min(self._config.heat_ceiling_percent, self._heat_engage_percent))

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
        return PostFcControlOutput(
            heat_percent=round(clamped),
            setpoint_c_per_min=setpoint,
            error_c_per_min=error,
            smoothed_ror_c_per_min=ema,
            integrator=self._integrator,
            effective_ceiling_percent=ceiling,
            effective_floor_percent=floor,
            saturated=saturated,
        )
