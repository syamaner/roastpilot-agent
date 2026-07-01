"""Deterministic post-first-crack RoR-target PI control loop (D82/D83, #405 Slice B).

This module implements the **algorithm only** — a pure, stateful, side-effect
free PI controller that holds a target bean rate-of-rise (RoR) band post-first-
crack by adjusting the heat lever. #405 Slice B1 built the algorithm: it does
not call :mod:`roastpilot_agent.controller`, :mod:`roastpilot_agent.safety`, or
:mod:`roastpilot_agent.control_policy`. Slice B2
(:meth:`roastpilot_agent.controller.RoastController._apply_deterministic_post_fc_levers`)
wires :class:`PostFcRorController` into the controller's DEVELOPMENT-phase
tick, builds the safety box from its actuated output (never an undamped target
— the #412 told==enforced control-path rule), and routes every resulting heat
command through the existing safety gate exactly like the advisor's commands
are routed today, all behind the ``PostFirstCrackControl.enabled`` flag
(default ``False``). **Nothing in this module writes to a roaster.**
``PostFcRorController`` never imports :mod:`roastpilot_agent.mcp_client` and
must never be wired to it directly.

Design (D83): a PI controller (proportional + integral, no derivative term) on
a smoothed (EMA) RoR signal, with conditional-integration anti-windup. The
controller is deliberately **stateful but deterministic**: all timing (``dt``)
is passed in by the caller rather than read from a clock, so the same input
sequence always produces the same output sequence — safe to replay and to
exercise in tests without a fake clock.

The output is always clamped to
``[PostFirstCrackControl.heat_floor_percent, PostFirstCrackControl.heat_ceiling_percent]``.
Because the floor is configured ``ge=1`` (never 0), a crash-to-0 heat command —
the roast-7 failure this loop exists to prevent — is structurally impossible
from this controller. The integrator uses conditional integration (a clamp
form of anti-windup): once the unclamped output saturates the rail, further
integration in the same direction is rolled back so the integrator cannot wind
up past the value that already holds the output at the rail, and can leave the
rail promptly once the error direction reverses (see :meth:`PostFcRorController.compute`).
"""

from dataclasses import dataclass

from pydantic import BaseModel

from roastpilot_agent.config import PostFirstCrackControl


@dataclass(frozen=True)
class PostFcControllerState:
    """An immutable snapshot of :class:`PostFcRorController`'s mutable state.

    Slice B2 (#405) needs this for the #412 told==enforced control-path rule
    applied to a *stateful* loop: the integrator/EMA must advance only after an
    ACCEPTED safety-gated write (ALLOW/CLAMP), never on a REJECT (e.g. a
    rate-limited tick) or any other non-actuated path. The caller takes a
    snapshot with :meth:`PostFcRorController.snapshot_state` **before** calling
    :meth:`PostFcRorController.compute`, and — only when the resulting command
    was rejected — restores it with :meth:`PostFcRorController.restore_state`
    so the tentative step is fully undone, as if ``compute`` had never run.

    Immutable and side-effect free: holding one of these carries no reference
    to the controller and cannot itself mutate it.
    """

    integrator: float
    bias_percent: float
    ema: float | None


class PostFcControlOutput(BaseModel, frozen=True):
    """One post-FC PI control step's result, with full diagnostics.

    Carries not just the actuated heat command but the internal state the
    caller (Slice B2's controller wiring), the decision trace, and tests need
    to see inside the loop: the smoothed RoR the error was computed from, the
    resulting error, the integrator's value after this step, and whether the
    output was saturated (clamped) this step.

    All temperatures are Celsius; RoR is °C/min; ``heat_percent`` is a
    percentage in ``[heat_floor_percent, heat_ceiling_percent]``.
    """

    #: The commanded heat percentage for this control step, already clamped to
    #: the configured ``[heat_floor_percent, heat_ceiling_percent]`` box and
    #: rounded to the nearest integer (Hottop levers are integer percentages).
    heat_percent: int
    #: ``target_ror_c_per_min - smoothed_ror_c_per_min`` for this step (before
    #: the deadband is applied to decide integration) — positive means RoR is
    #: too LOW (more heat needed), negative means too HIGH (less heat needed).
    error_c_per_min: float
    #: The EMA-smoothed RoR this step's error was computed from.
    smoothed_ror_c_per_min: float
    #: The integrator's accumulated value *after* this step (post anti-windup
    #: rollback), in the same units the loop scales by ``ki`` — i.e. accumulated
    #: (°C/min)·seconds of effective error.
    integrator: float
    #: Whether the unclamped PI output fell outside the configured heat box
    #: this step (the anti-windup rollback engaged when combined with
    #: continued integration in the saturating direction).
    saturated: bool


class PostFcRorController:
    """A pure, stateful PI controller holding a post-FC RoR target (D83).

    Holds the mutable loop state (the integrator and the EMA RoR estimate)
    across successive :meth:`compute` calls. Deterministic and side-effect
    free: every call is a pure function of ``self``'s current state plus the
    arguments passed in — no wall-clock reads, no I/O, no randomness — so a
    recorded sequence of ``(measured_ror_c_per_min, dt_seconds)`` pairs always
    replays to the exact same output sequence.

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
            config: The PI gains, RoR target/deadband, and heat/fan bounds
                (D83). The controller holds a reference to this config but
                never mutates it — all mutable state lives on ``self``.
        """
        self._config = config
        self._integrator: float = 0.0
        self._bias_percent: float = 0.0
        self._ema: float | None = None

    def reset(self, *, initial_heat_percent: int) -> None:
        """Seed the loop for a bumpless handoff at first-crack engagement.

        Clears the RoR EMA (the next :meth:`compute` call treats its first
        sample as the initial estimate, with no stale pre-FC RoR history) and
        seeds the loop's internal state so that a **zero-error** first
        :meth:`compute` call returns ``initial_heat_percent`` — i.e. the loop
        picks up exactly where the pre-FC heat level left off, with no heat
        dip or jump at the moment the loop takes over. This is bumpless
        transfer: without it, the loop would start from an empty integrator
        (implying 0 % heat) and the proportional term alone would have to make
        up the entire gap, producing a visible heat step at the exact tick the
        loop engages.

        Bumpless-transfer math: at zero error (post-deadband, ``error_eff ==
        0``) :meth:`compute`'s output formula is ``ki * integrator + bias``
        (the proportional term is 0 when ``error_eff`` is 0). This method
        seeds:

        * ``integrator = initial_heat_percent / ki`` when ``ki > 0`` (clamped
          to ``[0, heat_ceiling_percent / ki]`` — the same range the running
          integrator is bounded to by :meth:`compute`'s anti-windup, so a
          handoff heat outside the configured box cannot seed a value the
          loop could never reach unclamped anyway), with ``bias = 0``.
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
                (or the prior control state) was holding at the moment this
                loop takes over. Used only to seed the handoff bias; not
                otherwise validated against the config's box (a value outside
                the box is clamped the same way any other output is clamped,
                on the very first :meth:`compute` call).
        """
        self._ema = None
        ki = self._config.ki_percent_per_ror_second
        ceiling = self._config.heat_ceiling_percent
        if ki > 0:
            # Bound the seed the same way `compute`'s anti-windup bounds the
            # running integrator: never above what would drive the unclamped
            # output past the ceiling, never below 0 (a negative integrator
            # would imply a negative heat bias, which the floor already
            # guarantees is unreachable in practice since heat_floor >= 1).
            seeded = initial_heat_percent / ki
            self._integrator = max(0.0, min(seeded, ceiling / ki))
            self._bias_percent = 0.0
        else:
            # Degenerate ki==0 path (see docstring): there is no integrator
            # scaling that can hold a bias through the `ki * integrator` term,
            # so the handoff level is carried as an explicit constant instead.
            self._integrator = 0.0
            self._bias_percent = float(initial_heat_percent)

    def snapshot_state(self) -> PostFcControllerState:
        """Capture the loop's mutable state before a tentative :meth:`compute` step.

        Slice B2 (#405) calls this immediately before ``compute`` so a REJECTed
        (e.g. rate-limited) safety verdict can undo the tentative step with
        :meth:`restore_state` — the loop's integrator/EMA must advance only on
        an ACCEPTED (ALLOW/CLAMP) write, mirroring the pre-FC adaptive-trim
        "advance state only on accepted write" rule (#412).

        Returns:
            An immutable :class:`PostFcControllerState` capturing the current
            integrator, bias, and EMA.
        """
        return PostFcControllerState(
            integrator=self._integrator,
            bias_percent=self._bias_percent,
            ema=self._ema,
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

    def compute(self, *, measured_ror_c_per_min: float, dt_seconds: float) -> PostFcControlOutput:
        """Run one PI control step and return the commanded heat + diagnostics.

        Steps (D83):

        1. **EMA-smooth** the RoR sample: ``ema = alpha*measured +
           (1-alpha)*prev_ema``, or ``ema = measured`` on the very first call
           (no prior estimate to blend with).
        2. **Error:** ``error = target_ror - ema``. RoR too LOW (below target)
           gives a positive error, which — per the sign convention this loop
           uses — commands MORE heat; RoR too HIGH gives a negative error and
           commands LESS heat.
        3. **Deadband:** if ``abs(error) <= ror_deadband_c_per_min`` the loop
           HOLDS — ``error_eff`` is forced to 0.0 and the integrator does NOT
           accumulate this step (the #386/#412 lesson: a deadband that only
           gates the proportional term but keeps integrating would still
           slowly drift the output inside the band).
        4. Otherwise ``error_eff = error`` and the integrator tentatively
           accumulates ``error_eff * dt_seconds``.
        5. **Output:** ``bias + ki * integrator + kp * error_eff`` (unclamped).
           For a ``ki > 0`` loop the handoff bias lives entirely in the
           integrator seeded by :meth:`reset` and ``bias`` is 0, so this
           reduces to ``ki * integrator + kp * error_eff`` — at exactly zero
           error (post-deadband) the output is ``ki * integrator``, the held
           level. For the ``ki == 0`` degenerate case (see :meth:`reset`),
           ``bias`` alone carries the handoff level and the loop is pure-P
           around it.
        6. **Clamp** to ``[heat_floor_percent, heat_ceiling_percent]``;
           ``saturated`` is set when the unclamped output fell outside that
           range.
        7. **Anti-windup (conditional integration):** if the unclamped output
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
                method stays deterministic and replay-safe. Must be strictly
                positive (Slice B2 review note): a zero or negative ``dt``
                would either freeze or reverse the integrator's accumulated
                direction, which is never a valid tick duration — the caller
                is responsible for supplying a sane value (e.g. the configured
                ``control_interval_seconds`` on the very first post-handoff
                tick).

        Returns:
            The :class:`PostFcControlOutput` for this step: the clamped
            integer heat percentage plus the smoothed RoR, the error, the
            post-step integrator value, and whether this step saturated.

        Raises:
            ValueError: If ``dt_seconds`` is not strictly positive.
        """
        if dt_seconds <= 0.0:
            raise ValueError(f"dt_seconds must be > 0 ({dt_seconds})")
        config = self._config
        alpha = config.ror_smoothing_alpha
        ema = (
            measured_ror_c_per_min
            if self._ema is None
            else alpha * measured_ror_c_per_min + (1.0 - alpha) * self._ema
        )
        self._ema = ema

        error = config.target_ror_c_per_min - ema
        within_deadband = abs(error) <= config.ror_deadband_c_per_min
        error_eff = 0.0 if within_deadband else error

        pre_step_integrator = self._integrator
        tentative_integrator = pre_step_integrator + error_eff * dt_seconds

        unclamped = (
            self._bias_percent
            + config.ki_percent_per_ror_second * tentative_integrator
            + config.kp_percent_per_ror * error_eff
        )
        floor = config.heat_floor_percent
        ceiling = config.heat_ceiling_percent
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
            error_c_per_min=error,
            smoothed_ror_c_per_min=ema,
            integrator=self._integrator,
            saturated=saturated,
        )
