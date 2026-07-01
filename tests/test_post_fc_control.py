"""Tests for the deterministic post-FC RoR-target PI control loop (D82/D83,
#405 Slice B1).

Slice B1 is INERT — nothing in ``controller.py``/``safety.py`` calls
:class:`~roastpilot_agent.post_fc_control.PostFcRorController` yet — so these
tests exercise the algorithm directly: sign correctness, deadband holds, the
hard heat-floor/ceiling clamp (never 0 %), anti-windup bounding + prompt
recovery, bumpless-reset handoff, EMA smoothing, and determinism. Also covers
the ``PostFirstCrackControl`` config validator and its ``enabled`` default.
"""

import pytest

from roastpilot_agent.config import PostFirstCrackControl
from roastpilot_agent.post_fc_control import PostFcControlOutput, PostFcRorController


def _config(**overrides: object) -> PostFirstCrackControl:
    return PostFirstCrackControl(**overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_post_first_crack_control_disabled_by_default() -> None:
    config = PostFirstCrackControl()
    assert config.enabled is False


def test_post_first_crack_control_defaults() -> None:
    config = PostFirstCrackControl()
    assert config.target_ror_c_per_min == 8.0
    assert config.ror_deadband_c_per_min == 1.0
    assert config.kp_percent_per_ror == 3.0
    assert config.ki_percent_per_ror_second == 0.1
    assert config.heat_floor_percent == 25
    assert config.heat_ceiling_percent == 100
    assert config.fan_percent == 40
    assert config.control_interval_seconds == 5.0
    assert config.ror_smoothing_alpha == 0.4


def test_post_first_crack_control_heat_floor_above_ceiling_raises() -> None:
    with pytest.raises(ValueError, match="heat_floor_percent must not exceed heat_ceiling_percent"):
        PostFirstCrackControl(heat_floor_percent=50, heat_ceiling_percent=40)


def test_post_first_crack_control_heat_floor_equal_ceiling_is_valid() -> None:
    config = PostFirstCrackControl(heat_floor_percent=50, heat_ceiling_percent=50)
    assert config.heat_floor_percent == config.heat_ceiling_percent == 50


def test_post_first_crack_control_heat_floor_ge_one() -> None:
    """The floor's ``ge=1`` bound (never 0) is a config-level guarantee that a
    crash-to-0 heat command is structurally impossible — the roast-7 failure
    this loop exists to prevent."""
    with pytest.raises(ValueError):
        PostFirstCrackControl(heat_floor_percent=0)


# ---------------------------------------------------------------------------
# dt_seconds contract (Slice B2 review note, #405)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dt_seconds", [0.0, -1.0, -0.001])
def test_compute_rejects_non_positive_dt_seconds(dt_seconds: float) -> None:
    """A zero or negative ``dt_seconds`` would freeze or reverse the
    integrator's accumulated direction — never a valid tick duration. The
    caller (Slice B2's controller wiring) is responsible for supplying a sane
    value; this is the loop's own defensive contract."""
    controller = PostFcRorController(_config())
    controller.reset(initial_heat_percent=50)
    with pytest.raises(ValueError, match="dt_seconds must be > 0"):
        controller.compute(measured_ror_c_per_min=8.0, dt_seconds=dt_seconds)


# ---------------------------------------------------------------------------
# State snapshot/restore (Slice B2, #405: the #412 told==enforced rule
# extended to this stateful loop — the caller restores a pre-compute snapshot
# on any non-actuated write so the integrator/EMA never advance on a rejected
# command)
# ---------------------------------------------------------------------------


def test_restore_state_undoes_a_compute_step() -> None:
    config = _config(target_ror_c_per_min=8.0, ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    before = controller.snapshot_state()
    output_before = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    controller.restore_state(before)

    # Recomputing from the restored state must reproduce the exact same output
    # as the undone step — proof the snapshot/restore round-trips cleanly.
    output_after_restore = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    assert output_after_restore == output_before


def test_snapshot_state_is_immutable_and_independent_of_later_mutation() -> None:
    config = _config(target_ror_c_per_min=8.0, ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)
    snapshot = controller.snapshot_state()

    controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    controller.compute(measured_ror_c_per_min=25.0, dt_seconds=5.0)
    # The earlier snapshot's fields must be unaffected by the later computes.
    assert snapshot.integrator == pytest.approx(50.0 / config.ki_percent_per_ror_second)
    assert snapshot.bias_percent == 0.0
    assert snapshot.ema is None


# ---------------------------------------------------------------------------
# Sign correctness
# ---------------------------------------------------------------------------


def test_ror_below_target_raises_heat_above_handoff_level() -> None:
    config = _config(
        target_ror_c_per_min=8.0,
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    # RoR well below target (4 vs target 8, outside the deadband) -> error > 0
    # -> heat commanded ABOVE the 50% handoff level.
    output = controller.compute(measured_ror_c_per_min=4.0, dt_seconds=5.0)
    assert output.error_c_per_min > 0.0
    assert output.heat_percent > 50


def test_ror_above_target_lowers_heat_below_handoff_level() -> None:
    config = _config(
        target_ror_c_per_min=8.0,
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    # RoR well above target (14 vs target 8, outside the deadband) -> error < 0
    # -> heat commanded BELOW the 50% handoff level.
    output = controller.compute(measured_ror_c_per_min=14.0, dt_seconds=5.0)
    assert output.error_c_per_min < 0.0
    assert output.heat_percent < 50


# ---------------------------------------------------------------------------
# Deadband
# ---------------------------------------------------------------------------


def test_deadband_holds_output_and_does_not_move_integrator() -> None:
    config = _config(
        target_ror_c_per_min=8.0,
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60)

    # 8.5 is within +/-1.0 of target 8.0 -> inside the deadband.
    output = controller.compute(measured_ror_c_per_min=8.5, dt_seconds=5.0)
    assert output.heat_percent == 60
    assert output.integrator == pytest.approx(60.0 / config.ki_percent_per_ror_second)

    # A second in-deadband tick must not move the integrator further either.
    output2 = controller.compute(measured_ror_c_per_min=7.6, dt_seconds=5.0)
    assert output2.heat_percent == 60
    assert output2.integrator == pytest.approx(output.integrator)


def test_deadband_boundary_is_inclusive() -> None:
    """``abs(error) <= deadband`` — exactly at the boundary still holds."""
    config = _config(target_ror_c_per_min=8.0, ror_deadband_c_per_min=1.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=55)

    output = controller.compute(measured_ror_c_per_min=9.0, dt_seconds=5.0)  # error == -1.0
    assert output.heat_percent == 55


# ---------------------------------------------------------------------------
# Floor / ceiling clamp (never 0)
# ---------------------------------------------------------------------------


def test_huge_positive_error_never_exceeds_ceiling() -> None:
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    # RoR near 0 vs an 8.0 target -> a large positive error every tick.
    output = None
    for _ in range(50):
        output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert output is not None
    assert output.heat_percent <= 100
    assert output.heat_percent == 100
    assert output.saturated is True


def test_huge_negative_error_never_drops_below_floor_and_never_zero() -> None:
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    # A very hot RoR relative to an 8.0 target -> a large negative error.
    output = None
    for _ in range(50):
        output = controller.compute(measured_ror_c_per_min=40.0, dt_seconds=5.0)
    assert output is not None
    assert output.heat_percent >= 25
    assert output.heat_percent == 25
    assert output.heat_percent != 0
    assert output.saturated is True


# ---------------------------------------------------------------------------
# Anti-windup
# ---------------------------------------------------------------------------


def test_sustained_saturation_keeps_integrator_bounded() -> None:
    """A long run with RoR far below target pins heat at the ceiling; the
    integrator must not grow without bound (conditional-integration
    anti-windup rolls back the tentative accumulation while saturated)."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    integrators: list[float] = []
    last_output: PostFcControlOutput | None = None
    for _ in range(200):
        last_output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
        integrators.append(last_output.integrator)
    assert last_output is not None

    # The integrator must stabilize (not grow unboundedly): the last 50
    # samples vary by only a negligible amount once the rollback is in effect.
    tail = integrators[-50:]
    assert max(tail) - min(tail) < 1e-6
    assert last_output.heat_percent == 100  # sanity: still saturated


def test_recovers_promptly_after_saturation_when_ror_crosses_back() -> None:
    """After a long low-RoR saturation run, RoR crossing back above target
    must leave the ceiling promptly — no long windup lag."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100, ror_smoothing_alpha=1.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    last_output: PostFcControlOutput | None = None
    for _ in range(100):
        last_output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert last_output is not None
    assert last_output.heat_percent == 100
    assert last_output.saturated is True

    # RoR now well ABOVE target -> heat should drop off the ceiling within a
    # single tick (no backlog of accumulated integrator to unwind first).
    output = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    assert output.heat_percent < 100


def test_anti_windup_bounded_integrator_compared_to_naive_accumulation() -> None:
    """Without anti-windup a plain integrator accumulating
    ``error * dt_seconds`` every tick over 200 ticks at dt=5s with error ~8
    would reach ~8000; the anti-windup-bounded integrator must stay far below
    that (it freezes once saturated)."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    naive_unbounded_integrator_estimate = 8.0 * 5.0 * 200  # error(~8) * dt * ticks
    output = None
    for _ in range(200):
        output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert output is not None
    assert output.integrator < naive_unbounded_integrator_estimate / 10


# ---------------------------------------------------------------------------
# Bumpless reset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("initial_heat", [25, 40, 55, 70, 100])
def test_bumpless_reset_holds_handoff_heat_at_zero_error(initial_heat: int) -> None:
    config = _config(target_ror_c_per_min=8.0, ror_smoothing_alpha=1.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=initial_heat)

    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    assert output.heat_percent == pytest.approx(initial_heat, abs=1)


def test_bumpless_reset_with_ki_zero_degenerate_path() -> None:
    """When ``ki == 0`` the loop is pure-P; :meth:`reset` stores the initial
    heat directly so the very first zero-error compute still reproduces it
    (see the ``ki == 0`` docstring note in ``PostFcRorController.reset``)."""
    config = _config(
        ki_percent_per_ror_second=0.0, target_ror_c_per_min=8.0, ror_smoothing_alpha=1.0
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60)

    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    assert output.heat_percent == 60


def test_bumpless_reset_clears_ema() -> None:
    """A stale RoR EMA from a previous engagement must not leak across a reset
    (the first post-reset sample becomes the new EMA baseline unblended)."""
    config = _config(target_ror_c_per_min=8.0, ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)
    controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)  # pollute the EMA

    controller.reset(initial_heat_percent=50)
    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    # If the stale EMA (blended toward 20.0) had leaked, the smoothed RoR would
    # not equal the fresh sample exactly.
    assert output.smoothed_ror_c_per_min == 8.0


# ---------------------------------------------------------------------------
# EMA smoothing
# ---------------------------------------------------------------------------


def test_ema_smoothing_dampens_a_single_ror_spike_vs_no_smoothing() -> None:
    smoothed_config = _config(ror_smoothing_alpha=0.4, target_ror_c_per_min=8.0)
    raw_config = _config(ror_smoothing_alpha=1.0, target_ror_c_per_min=8.0)

    smoothed_controller = PostFcRorController(smoothed_config)
    raw_controller = PostFcRorController(raw_config)
    smoothed_controller.reset(initial_heat_percent=50)
    raw_controller.reset(initial_heat_percent=50)

    # Settle both at the target RoR first so the EMA baseline equals target.
    smoothed_controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    raw_controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)

    # A single spike far from target.
    spike_smoothed = smoothed_controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    spike_raw = raw_controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)

    assert abs(spike_smoothed.error_c_per_min) < abs(spike_raw.error_c_per_min)
    # The smoothed controller's heat move is therefore smaller in magnitude.
    assert abs(spike_smoothed.heat_percent - 50) < abs(spike_raw.heat_percent - 50)


def test_ema_first_sample_has_no_prior_estimate() -> None:
    config = _config(ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    output = controller.compute(measured_ror_c_per_min=12.0, dt_seconds=5.0)
    assert output.smoothed_ror_c_per_min == 12.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_outputs() -> None:
    config = _config()

    def _run() -> list[PostFcControlOutput]:
        controller = PostFcRorController(config)
        controller.reset(initial_heat_percent=50)
        inputs = [(6.0, 5.0), (7.5, 5.0), (9.0, 5.0), (12.0, 4.0), (3.0, 6.0), (8.0, 5.0)]
        return [controller.compute(measured_ror_c_per_min=ror, dt_seconds=dt) for ror, dt in inputs]

    first_run = _run()
    second_run = _run()
    assert first_run == second_run


# ---------------------------------------------------------------------------
# Output invariants
# ---------------------------------------------------------------------------


def test_output_heat_percent_always_within_configured_box() -> None:
    config = _config(heat_floor_percent=30, heat_ceiling_percent=90)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    sequence = [0.0, 2.0, 30.0, 8.0, 8.0, 100.0, -5.0, 8.5]
    for ror in sequence:
        output = controller.compute(measured_ror_c_per_min=ror, dt_seconds=5.0)
        assert 30 <= output.heat_percent <= 90
        assert output.heat_percent != 0


# ---------------------------------------------------------------------------
# Anti-windup: the "saturated but climbing toward the box" branch (Opus
# safety-review finding — was wrongly tagged unreachable). Pinned at the floor
# with a positive error, the integrator must KEEP climbing (not roll back).
# ---------------------------------------------------------------------------


def test_below_floor_with_positive_error_keeps_integrating_toward_floor() -> None:
    """Saturated at the FLOOR while still climbing (positive error) must NOT
    roll the integrator back — the loop keeps working toward the box.

    Regression for the mis-tagged ``# pragma: no cover`` branch: an Opus safety
    review disproved the "unreachable" claim by fuzzing (5522 hits). It is a
    normal wide-box post-FC transient, reproduced deterministically here with a
    low handoff seed so the unclamped output lands below the floor.
    """
    config = _config(
        heat_floor_percent=30,
        heat_ceiling_percent=100,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
    )  # target 8.0 / deadband 1.0 (defaults)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=5)  # integrator seeded low (5/ki = 50)

    # error = +2 (RoR below target); tentative integrator 50 -> 60;
    # unclamped = ki*60 + kp*2 = 6 + 6 = 12 < floor 30.
    output = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert output.saturated is True
    assert output.heat_percent == 30  # pinned at the floor
    # Integrator kept the tentative climb (50 -> 60), NOT rolled back to 50.
    assert output.integrator == pytest.approx(60.0)

    # It keeps climbing toward the box on the next tick (the loop is working,
    # not wound-up-frozen below the floor).
    output2 = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert output2.integrator > output.integrator


def test_saturated_flag_false_when_output_within_box() -> None:
    """The ``saturated`` flag is explicitly False for an in-box output (qa
    strengthening — the flag was only ever asserted True elsewhere)."""
    config = _config()  # floor 25 / ceiling 100
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50)

    # At exactly the target RoR the error is within the deadband; output holds
    # at the seeded handoff level (ki*integrator = 0.1 * 500 = 50), in-box.
    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    assert output.saturated is False
    assert output.heat_percent == 50
