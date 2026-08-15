"""Tests for the deterministic post-FC RoR-taper PI control loop (D82/D88,
#405 Slice B).

D88 replaced D83's fixed-band setpoint with a taper anchored to the MEASURED
engagement RoR after a hardware A/B (roasts 9/10,
``docs/analysis/2026-07-09-roast9-10-postfc-ab.md``) showed the fixed-band law
actuating a runaway 72->91% heat climb while the advisor recommended 0%. These
tests exercise the algorithm directly (nothing in ``controller.py``/
``safety.py`` calls it yet at this Slice's boundary beyond the bumpless-handoff
wiring): the taper setpoint/clamp math (the roast-2 regression walk, the B1/B2
ratification edges), the never-add-heat-beyond-entry ceiling, the actuation
clock discipline (C1), sign correctness, deadband holds, anti-windup bounding +
prompt recovery, bumpless-reset handoff, EMA smoothing, and determinism. Also
covers the ``PostFirstCrackControl`` config validators and its ``enabled``
default.
"""

import dataclasses

import pytest

from roastpilot_agent.config import PostFirstCrackControl
from roastpilot_agent.post_fc_control import (
    PostFcControlOutput,
    PostFcHeatAuthorityState,
    PostFcProjectionInputs,
    PostFcRecoveryTrigger,
    PostFcRorController,
)


def _config(**overrides: object) -> PostFirstCrackControl:
    return PostFirstCrackControl(**overrides)  # type: ignore[arg-type]


def _projection(
    *,
    bean_temp_c: float = 180.0,
    target_drop_temp_c: float = 200.0,
    target_development_percent: float = 16.0,
    development_elapsed_seconds: float | None = 60.0,
    charge_elapsed_seconds: float = 500.0,
) -> PostFcProjectionInputs:
    """Build a valid, deliberately short recovery-v2 projection."""
    return PostFcProjectionInputs(
        bean_temp_c=bean_temp_c,
        target_drop_temp_c=target_drop_temp_c,
        target_development_percent=target_development_percent,
        development_elapsed_seconds=development_elapsed_seconds,
        charge_elapsed_seconds=charge_elapsed_seconds,
    )


def _projection_recovery_config(**overrides: object) -> PostFirstCrackControl:
    """Build the explicitly enabled v2 configuration used by focused tests."""
    values: dict[str, object] = {
        "recovery_enabled": True,
        "recovery_projection_enabled": True,
        "recovery_confirm_ticks": 3,
        "ror_smoothing_alpha": 1.0,
        "kp_percent_per_ror": 0.0,
    }
    values.update(overrides)
    return _config(**values)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_post_first_crack_control_enabled_by_default() -> None:
    """12 Jul D88/D89 promotion (operator-ratified): the RoR-taper loop is now
    the default — the 11 Jul validation roast passed structurally and the cup
    scored 9/10. Was ``False`` (advisor-driven post-FC default) before the
    flip; deliberately updated, not silently passed."""
    config = PostFirstCrackControl()
    assert config.enabled is True
    assert config.ceiling_guard_drop_enabled is True


def test_post_first_crack_control_defaults() -> None:
    config = PostFirstCrackControl()
    assert config.taper_start_max_ror_c_per_min == 8.0
    assert config.taper_end_ror_c_per_min == 4.0
    assert config.taper_duration_seconds == 90.0
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


def test_post_first_crack_control_taper_end_above_start_max_raises() -> None:
    with pytest.raises(
        ValueError, match="taper_end_ror_c_per_min must not exceed taper_start_max_ror_c_per_min"
    ):
        PostFirstCrackControl(taper_start_max_ror_c_per_min=4.0, taper_end_ror_c_per_min=8.0)


def test_post_first_crack_control_taper_end_equal_start_max_is_valid() -> None:
    config = PostFirstCrackControl(taper_start_max_ror_c_per_min=6.0, taper_end_ror_c_per_min=6.0)
    assert config.taper_start_max_ror_c_per_min == config.taper_end_ror_c_per_min == 6.0


# ---------------------------------------------------------------------------
# dt_seconds contract (Slice B2 review note, #405)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dt_seconds", [0.0, -1.0, -0.001])
def test_compute_rejects_non_positive_dt_seconds(dt_seconds: float) -> None:
    """A zero or negative ``dt_seconds`` would freeze or reverse the
    integrator's accumulated direction — never a valid tick duration. The
    caller is responsible for supplying a sane value; this is the loop's own
    defensive contract."""
    controller = PostFcRorController(_config())
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.0)
    with pytest.raises(ValueError, match="dt_seconds must be > 0"):
        controller.compute(measured_ror_c_per_min=8.0, dt_seconds=dt_seconds)


# ---------------------------------------------------------------------------
# State snapshot/restore (the #412 told==enforced rule extended to this
# stateful loop — the caller restores a pre-compute snapshot on any
# non-actuated write so the integrator/EMA/taper clock never advance on a
# rejected command)
# ---------------------------------------------------------------------------


def test_restore_state_undoes_a_compute_step() -> None:
    config = _config(ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.0)

    before = controller.snapshot_state()
    output_before = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    controller.restore_state(before)

    # Recomputing from the restored state must reproduce the exact same output
    # as the undone step — proof the snapshot/restore round-trips cleanly.
    output_after_restore = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    assert output_after_restore == output_before


def test_snapshot_state_is_immutable_and_independent_of_later_mutation() -> None:
    config = _config(ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.0)
    snapshot = controller.snapshot_state()

    controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    controller.compute(measured_ror_c_per_min=25.0, dt_seconds=5.0)
    # The earlier snapshot's fields must be unaffected by the later computes.
    assert snapshot.integrator == pytest.approx(50.0 / config.ki_percent_per_ror_second)
    assert snapshot.bias_percent == 0.0
    assert snapshot.ema is None
    assert snapshot.taper_elapsed_seconds == 0.0
    assert snapshot.taper_r0_c_per_min == pytest.approx(6.0)
    assert snapshot.heat_engage_percent == 50


def test_restore_state_undoes_taper_clock_advance() -> None:
    """The taper's elapsed clock is tentative like every other mutation in
    ``compute`` — a restore fully undoes it, so the setpoint at the next
    compute is exactly what it would have been had the undone step never
    run (C1/#412 discipline)."""
    config = _config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    before = controller.snapshot_state()
    controller.compute(measured_ror_c_per_min=6.1, dt_seconds=30.0)  # taper clock -> 30s
    controller.restore_state(before)

    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    # Only 5s have elapsed since the (restored) engagement, not 35s — proof
    # the 30s advance was fully undone.
    expected_progress = 5.0 / config.taper_duration_seconds
    expected_setpoint = 6.1 + expected_progress * (4.0 - 6.1)
    assert output.setpoint_c_per_min == pytest.approx(expected_setpoint)


# ---------------------------------------------------------------------------
# D88 taper setpoint math
# ---------------------------------------------------------------------------


def test_r0_anchors_to_measured_engagement_ror_when_in_range() -> None:
    config = _config(taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=4.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # r0 == 6.1 (measured), not the fixed 8.0 D83 default -> at zero elapsed
    # time the setpoint equals the measured engagement RoR exactly.
    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=0.001)
    assert output.setpoint_c_per_min == pytest.approx(6.1, abs=1e-3)


def test_r0_capped_at_taper_start_max_for_a_hot_engagement() -> None:
    config = _config(taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=4.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=15.0)  # unusually hot

    output = controller.compute(measured_ror_c_per_min=15.0, dt_seconds=0.001)
    assert output.setpoint_c_per_min == pytest.approx(8.0, abs=1e-3)


def test_taper_decays_linearly_from_r0_to_end_value() -> None:
    """Exercises the taper interpolation formula directly with a single large
    ``dt_seconds`` step -- ``control_interval_seconds`` is set generously
    above that step so the gap-resume cap (below) does not interfere; the cap
    itself has its own dedicated test,
    ``test_c1_gap_resume_dt_is_capped_to_one_control_interval``."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        control_interval_seconds=45.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # Advance to the exact midpoint of the 90s taper.
    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=45.0)
    expected_midpoint = 6.1 + 0.5 * (4.0 - 6.1)
    assert output.setpoint_c_per_min == pytest.approx(expected_midpoint)


def test_taper_holds_at_end_value_once_duration_elapses() -> None:
    """See ``test_taper_decays_linearly_from_r0_to_end_value``'s note on
    ``control_interval_seconds`` and the gap-resume cap."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        control_interval_seconds=90.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    controller.compute(measured_ror_c_per_min=6.1, dt_seconds=90.0)  # exactly at duration
    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=60.0)  # well past duration
    assert output.setpoint_c_per_min == pytest.approx(4.0)


def test_taper_setpoint_never_undershoots_the_end_value() -> None:
    """Linear interpolation with a floored r0 cannot overshoot past the end
    value even arbitrarily far past the taper duration. See
    ``test_taper_decays_linearly_from_r0_to_end_value``'s note on
    ``control_interval_seconds`` and the gap-resume cap."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=10.0,
        control_interval_seconds=1000.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=1000.0)
    assert output.setpoint_c_per_min == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# B1 (ratification): a degenerate low/negative engagement RoR floors r0 at
# the taper's own end value — never below it.
# ---------------------------------------------------------------------------


def test_b1_negative_engagement_ror_floors_r0_at_end_value() -> None:
    """A post-charge-crash FC can read a negative RoR at engagement. r0 must
    floor at taper_end (4.0), never sit below it (else tick-1 reads a
    spurious 'too hot' error and over-cuts)."""
    config = _config(taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=4.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=-3.0)

    output = controller.compute(measured_ror_c_per_min=-3.0, dt_seconds=0.001)
    assert output.setpoint_c_per_min == pytest.approx(4.0, abs=1e-3)


def test_b1_degenerate_engagement_tick1_is_not_an_instant_floor_cut() -> None:
    """B1's point: tick-1 output must not instantly crash to the floor just
    because the engagement RoR was degenerate. r0 floors at 4.0 (not below),
    so the tick-1 error against a measured RoR of -3.0 is bounded, and the
    resulting output stays well above a floor-cut."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
        heat_floor_percent=25,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=-3.0)

    output = controller.compute(measured_ror_c_per_min=-3.0, dt_seconds=5.0)
    # error = r0(4.0) - (-3.0) = 7.0 -> POSITIVE (more heat, not a cut). The
    # gentle first correction raises heat toward the ceiling, never crashes.
    assert output.error_c_per_min > 0.0
    assert output.heat_percent > 25  # nowhere near an instant floor-cut


# ---------------------------------------------------------------------------
# B2 (ratification): never-add-heat-beyond-entry, with the 1% anti-stall
# floor winning over it.
# ---------------------------------------------------------------------------


def test_effective_ceiling_is_the_heat_at_engagement() -> None:
    config = _config(heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # Even a huge positive error (RoR far below setpoint) cannot push heat
    # above 72 -- the roast never held more than 72% at engagement.
    output = None
    for _ in range(50):
        output = controller.compute(measured_ror_c_per_min=-10.0, dt_seconds=5.0)
    assert output is not None
    assert output.effective_ceiling_percent == 72
    assert output.heat_percent <= 72
    assert output.heat_percent == 72
    assert output.saturated is True


def test_effective_ceiling_never_exceeds_static_heat_ceiling() -> None:
    """A heat_engage above the static heat_ceiling_percent (e.g. a config
    edited between roasts) cannot push the effective ceiling past the
    static bound either."""
    config = _config(heat_ceiling_percent=80)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=95, ror_at_engagement_c_per_min=6.1)

    output = controller.compute(measured_ror_c_per_min=-10.0, dt_seconds=5.0)
    assert output.effective_ceiling_percent == 80


def test_b2_zero_heat_engagement_ceiling_is_one_not_zero() -> None:
    """B2's anti-stall floor: a 0% heat-at-engagement handoff must not pin
    the effective ceiling (and therefore the whole DEVELOPMENT dwell) at 0%
    — it wins to 1%."""
    config = _config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=0, ror_at_engagement_c_per_min=6.1)

    output = controller.compute(measured_ror_c_per_min=-10.0, dt_seconds=5.0)
    assert output.effective_ceiling_percent == 1
    assert output.heat_percent != 0


def test_b2_effective_floor_collapses_downward_with_a_low_ceiling() -> None:
    """When heat_engage pulls the effective ceiling below the STATIC
    heat_floor_percent, the effective floor must collapse down to match it
    (never leave floor > ceiling, an empty box)."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=10, ror_at_engagement_c_per_min=6.1)  # ceiling -> 10

    output = controller.compute(measured_ror_c_per_min=-10.0, dt_seconds=5.0)
    assert output.effective_ceiling_percent == 10
    assert output.effective_floor_percent == 10  # collapsed down from the static 25
    assert output.heat_percent == 10


# ---------------------------------------------------------------------------
# The roast-2 regression walk (measured numbers) — the point of D88.
# ---------------------------------------------------------------------------


def test_roast2_runaway_is_structurally_impossible() -> None:
    """Reproduce the measured roast-2 engagement (RoR 6.1, heat 72%) against
    the D88 default config (start-cap 8.0 / end 4.0). Tick 1: r0 == 6.1, the
    measured RoR IS the setpoint -> zero error -> output holds at the
    bumpless 72% (not a runaway climb toward 91%, D83's failure). As the
    measured RoR stays at or above the (decaying) taper setpoint across a
    simulated post-FC dwell, the output NEVER exceeds 72% (the
    never-add-heat-beyond-entry ceiling), and it eases DOWNWARD as the taper
    decays -- the opposite of the measured 72->91% climb.

    This test pins the ceiling side of the never-add-heat-beyond-entry clamp
    (output can never RISE past heat-at-engagement); its companion
    ``test_roast2_stall_recovery_never_exceeds_heat_at_engagement`` pins the
    recovery side (output can FALL below and then climb back, but still never
    past the same ceiling) -- together they bound both directions. Deleting
    either one leaves half the clamp unproven (a dropped ceiling that only
    ever matters when recovering from below would slip through this test
    alone).

    **D96 (#559) note:** this config does not set ``recovery_enabled`` and so
    defaults to ``False`` — the D96 recovery law is completely inert here,
    and ``effective_ceiling_percent`` is asserted to stay EXACTLY the D88
    value (72) on every tick, never the recovery ceiling, proving the two
    laws do not interact when recovery is off (the default) — a future
    change that accidentally made recovery active by default, or too eager
    even while nominally off, would break this existing D88 regression test
    directly rather than only a new D96-specific one."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # Tick 1: r0 == 6.1 == measured -> zero error -> bumpless hold at 72.
    tick1 = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    assert tick1.setpoint_c_per_min == pytest.approx(6.1, abs=0.2)
    assert tick1.heat_percent == 72
    assert tick1.error_c_per_min == pytest.approx(0.0, abs=0.5)
    assert tick1.effective_ceiling_percent == 72
    assert tick1.recovery_active is False

    # Simulate the measured RoR gently decaying alongside the taper (a
    # roast responding as expected to the held-then-eased heat), roughly
    # tracking the setpoint each tick.
    heats = [tick1.heat_percent]
    ceilings = [tick1.effective_ceiling_percent]
    ror = 6.1
    for _ in range(16):  # 16 * 5s = 80s, most of the 90s taper
        output = controller.compute(measured_ror_c_per_min=ror, dt_seconds=5.0)
        heats.append(output.heat_percent)
        ceilings.append(output.effective_ceiling_percent)
        ror = max(4.0, ror - 0.15)  # RoR eases down, tracking the taper

    # The 91% runaway is structurally impossible: the ceiling is 72 for the
    # entire engagement.
    assert all(h <= 72 for h in heats), heats
    # D96: with recovery off, the effective ceiling stays EXACTLY 72 (D88's
    # never-add-heat-beyond-entry value) on every tick, never a recovery
    # ceiling.
    assert all(c == 72 for c in ceilings), ceilings
    # And the output actually eases DOWN over the window (the opposite of
    # the measured climb) -- the last heat is no higher than the first.
    assert heats[-1] <= heats[0]


def test_roast2_stall_recovery_never_exceeds_heat_at_engagement() -> None:
    """The opposite failure direction: RoR runs HOT for a while (heat eases
    below the 72% ceiling), then STALLS (collapses). The loop must recover
    (raise heat back up) but never past the never-add-heat-beyond-entry
    ceiling (72) — proving recovery is bounded in both directions at once.

    This test pins the recovery side of the never-add-heat-beyond-entry clamp
    (output can fall below heat-at-engagement and climb back, but still never
    past it); its companion ``test_roast2_runaway_is_structurally_impossible``
    pins the ceiling side (output starts pinned at the ceiling and can only
    ease off it, never rise past it) -- together they bound both directions.
    Deleting either one leaves half the clamp unproven.

    **D96 (#559) note:** this config does not set ``recovery_enabled`` and so
    defaults to ``False`` — even though Phase 2's stalled RoR is EXACTLY the
    kind of persistent below-setpoint shortfall the D96 recovery law would
    react to, the recovery ceiling must never activate here (this is the
    RoR-taper's own D88 anti-stall recovery, unrelated to and unaffected by
    the separately-flagged D96 law) — ``effective_ceiling_percent`` is
    asserted to stay EXACTLY 72 on every tick of both phases."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # Phase 1: RoR runs well above the setpoint -> heat eases DOWN off the 72
    # handoff (never above 72 the whole time, since it started right at the
    # ceiling and can only ease off it here).
    phase1_heats: list[int] = []
    phase1_ceilings: list[int] = []
    for _ in range(10):
        output = controller.compute(measured_ror_c_per_min=12.0, dt_seconds=5.0)
        phase1_heats.append(output.heat_percent)
        phase1_ceilings.append(output.effective_ceiling_percent)
    assert phase1_heats[-1] < 72  # genuinely eased below the ceiling
    assert all(c == 72 for c in phase1_ceilings), phase1_ceilings

    # Phase 2: RoR collapses (stall) -> the loop recovers (raises heat back
    # up) but never past the never-add-heat-beyond-entry ceiling (72).
    phase2_heats: list[int] = []
    phase2_ceilings: list[int] = []
    phase2_recovery_active: list[bool] = []
    for _ in range(20):
        output = controller.compute(measured_ror_c_per_min=2.0, dt_seconds=5.0)  # stalled RoR
        phase2_heats.append(output.heat_percent)
        phase2_ceilings.append(output.effective_ceiling_percent)
        phase2_recovery_active.append(output.recovery_active)

    assert max(phase2_heats) <= 72
    assert phase2_heats[-1] > phase1_heats[-1]  # recovering, not frozen
    # D96: recovery-off means the ceiling stays exactly 72 (D88's value) even
    # through the sustained stall Phase 2 simulates, and recovery_active never
    # flips True.
    assert all(c == 72 for c in phase2_ceilings), phase2_ceilings
    assert all(a is False for a in phase2_recovery_active)


# ---------------------------------------------------------------------------
# C1 (ratification): the taper clock advances on the ACTUATION clock only.
# ---------------------------------------------------------------------------


def test_c1_a_gap_with_no_compute_call_does_not_advance_the_taper() -> None:
    """A caller that skips ``compute`` entirely across a gap (e.g. every tick
    in the gap was REJECTed before ever calling compute) leaves the taper's
    internal clock untouched -- the next compute call's dt_seconds reflects
    only the ACTUATION-clock elapsed time, never the wall-clock gap.

    This test covers a gap where ``compute`` is never called at all. Its
    companion ``test_c1_gap_resume_dt_is_capped_to_one_control_interval``
    covers the OTHER C1 exposure: a gap where ``compute`` IS eventually
    called again, but with a ``dt_seconds`` spanning the whole outage (the
    controller's RoR-unavailable fail-closed path never advances the
    actuation clock either) -- together they cover both ways a gap can reach
    this method without silently marching the setpoint down."""
    config = _config(taper_duration_seconds=90.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # First accepted actuation, 5s after engagement.
    first = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    # A long real-world gap passes (say 300s of rejects/holds) with NO
    # compute call during it -- the loop is never even asked to advance.
    # The next accepted actuation's dt_seconds is only the ACTUATION-clock
    # elapsed time since the last accepted call (5s here), not 300s.
    second = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    assert second.setpoint_c_per_min < first.setpoint_c_per_min  # only ~10s total elapsed
    # 10s elapsed total (not 305s) -> still very early in a 90s taper.
    expected = 6.1 + (10.0 / 90.0) * (4.0 - 6.1)
    assert second.setpoint_c_per_min == pytest.approx(expected)


def test_c1_rejected_write_restore_prevents_taper_advance() -> None:
    """The controller-level discipline: snapshot before compute, restore on a
    REJECTed/failed write. The taper elapsed clock is restored right along
    with the integrator/EMA, so a rejected tick's tentative compute leaves NO
    trace on the next accepted actuation's setpoint."""
    config = _config(taper_duration_seconds=90.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    snapshot = controller.snapshot_state()
    controller.compute(measured_ror_c_per_min=6.1, dt_seconds=45.0)  # tentative, e.g. rate-limited
    controller.restore_state(snapshot)  # the write was rejected -> undo

    output = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    # Only 5s have elapsed total, not 50s.
    expected = 6.1 + (5.0 / 90.0) * (4.0 - 6.1)
    assert output.setpoint_c_per_min == pytest.approx(expected)


def test_c1_gap_resume_dt_is_capped_to_one_control_interval() -> None:
    """Codex finding (gap-swallow, #405 PR): unlike a rejected/no-compute gap
    (this test's companion, ``test_c1_a_gap_with_no_compute_call_does_not_advance_the_taper``,
    covers that case), a RoR-unavailable outage IS eventually followed by a
    real ``compute`` call once RoR returns -- but the controller's
    fail-closed guard never advances ``_post_fc_last_actuation_monotonic``
    while RoR is missing, so that first post-outage call would otherwise
    receive a ``dt_seconds`` spanning the WHOLE outage. This method must cap
    what it advances state by to at most one ``control_interval_seconds``,
    so a 60s outage does not jump the taper (or the integrator) 60s forward
    in a single step -- only by one interval, matching what a normally-paced
    tick loop could actually have observed."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        control_interval_seconds=5.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)

    # One normal accepted actuation at the engagement RoR (bumpless hold).
    first = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)
    assert first.heat_percent == 72

    # A 60s RoR outage follows (many ticks with bean_ror_c_per_min is None at
    # the controller level -- this loop is never even called during them).
    # The FIRST call after the outage receives dt_seconds=60.0 (the elapsed
    # time since the last accepted actuation), NOT a value pre-clamped by the
    # caller -- this method's own clamp must protect it.
    after_gap = controller.compute(measured_ror_c_per_min=6.1, dt_seconds=60.0)

    # The taper must advance by AT MOST one control_interval_seconds (5s),
    # not the full 60s outage: total elapsed after this call is (5 + 5) = 10s,
    # not (5 + 60) = 65s.
    expected_setpoint = 6.1 + (10.0 / 90.0) * (4.0 - 6.1)
    assert after_gap.setpoint_c_per_min == pytest.approx(expected_setpoint)
    # And a setpoint that only advanced by one interval's worth of decay
    # produces only a negligible move off the bumpless 72% hold -- nowhere
    # near the 60-point crash an uncapped 60s integration step would cause
    # (empirically: an uncapped 60s dt on this exact scenario drops heat to
    # 60%; the capped version must stay far closer to 72).
    assert after_gap.heat_percent >= 70


# ---------------------------------------------------------------------------
# C2 (ratification): snapshot/restore preserves taper state; a fresh engage
# re-captures from scratch.
# ---------------------------------------------------------------------------


def test_c2_snapshot_restore_preserves_taper_state_mid_episode() -> None:
    """Ground-truth check (qa finding): restoring a mid-episode snapshot into
    a fresh controller must reproduce the SAME next-tick output an
    uninterrupted controller would have produced at the identical elapsed
    time — not merely agree with another restore of the same snapshot (two
    controllers restored from the same buggy snapshot would "agree" with each
    other while both being wrong; only a comparison against an uninterrupted
    run proves the snapshot round-trip is faithful to ground truth).

    ``control_interval_seconds`` is set to match the 30s step used below so
    this is a normally-paced cadence, not a gap -- the gap-resume cap has its
    own dedicated test, ``test_c1_gap_resume_dt_is_capped_to_one_control_interval``."""
    config = _config(control_interval_seconds=30.0)

    # Ground truth: one controller run uninterrupted end-to-end (engage, then
    # 30s + 5s = 35s total elapsed at the comparison tick).
    ground_truth_controller = PostFcRorController(config)
    ground_truth_controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)
    ground_truth_controller.compute(measured_ror_c_per_min=6.1, dt_seconds=30.0)
    ground_truth = ground_truth_controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)

    # Scenario under test: snapshot at the 30s mark, restore into a FRESH
    # controller instance (simulating a same-process save/restore), then
    # compute the same next 5s tick.
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)
    controller.compute(measured_ror_c_per_min=6.1, dt_seconds=30.0)

    mid_episode = controller.snapshot_state()
    assert mid_episode.taper_elapsed_seconds == pytest.approx(30.0)
    assert mid_episode.taper_r0_c_per_min == pytest.approx(6.1)
    assert mid_episode.heat_engage_percent == 72

    restored_controller = PostFcRorController(config)
    restored_controller.restore_state(mid_episode)
    resumed = restored_controller.compute(measured_ror_c_per_min=6.1, dt_seconds=5.0)

    # The restored controller's output must match the UNINTERRUPTED ground
    # truth at the same elapsed time — not just another restore of the same
    # snapshot (a mutual-agreement check alone cannot catch a bug shared by
    # both restores, e.g. a corrupted taper_elapsed_seconds baked into the
    # snapshot itself).
    assert resumed == ground_truth


def test_c2_fresh_engage_recaptures_and_does_not_inherit_prior_episode() -> None:
    """A new :meth:`reset` call (a fresh true-FC-edge engagement) always
    re-captures r0/heat_engage/elapsed from scratch -- it must never inherit
    a prior engagement's taper state, even on the SAME controller instance."""
    config = _config()
    controller = PostFcRorController(config)

    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=6.1)
    controller.compute(measured_ror_c_per_min=6.1, dt_seconds=60.0)  # far into the old taper

    # A fresh engagement (a new roast's true FC edge) re-seeds everything.
    controller.reset(initial_heat_percent=55, ror_at_engagement_c_per_min=7.0)
    snapshot = controller.snapshot_state()
    assert snapshot.taper_elapsed_seconds == 0.0
    assert snapshot.taper_r0_c_per_min == pytest.approx(7.0)
    assert snapshot.heat_engage_percent == 55

    output = controller.compute(measured_ror_c_per_min=7.0, dt_seconds=0.001)
    assert output.setpoint_c_per_min == pytest.approx(7.0, abs=1e-3)
    assert output.effective_ceiling_percent == 55


# ---------------------------------------------------------------------------
# OFF no-op (flag False leaves the whole regime byte-for-byte unchanged) is
# proven at the controller-wiring level in test_controller.py; this module's
# OFF-no-op guarantee is that the config's ``enabled`` default is False and
# nothing here is called unless a caller explicitly constructs the loop.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sign correctness
# ---------------------------------------------------------------------------


def test_ror_below_setpoint_pushes_unclamped_output_above_handoff_level() -> None:
    """RoR below the taper's setpoint drives a POSITIVE error, which (per the
    loop's sign convention) pushes the unclamped output UP. Under D88 the
    clamped output can never actually exceed the handoff heat when the
    handoff heat sits at the effective ceiling (never-add-heat-beyond-entry)
    — this test proves the direction is right (saturated at the ceiling,
    wanting to go higher), not that the clamped value rises past it."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=8.0,  # pin the setpoint flat at 8.0 for this test
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)

    # RoR well below the (flat) setpoint (4 vs 8, outside the deadband) ->
    # error > 0 -> the loop pushes toward MORE heat, saturating at the
    # never-add-heat-beyond-entry ceiling (50, the handoff level) rather than
    # climbing past it.
    output = controller.compute(measured_ror_c_per_min=4.0, dt_seconds=5.0)
    assert output.error_c_per_min > 0.0
    assert output.saturated is True
    assert output.heat_percent == output.effective_ceiling_percent == 50


def test_ror_above_setpoint_lowers_heat_below_handoff_level() -> None:
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=8.0,  # pin the setpoint flat at 8.0 for this test
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)

    # RoR well above the (flat) setpoint (14 vs 8, outside the deadband) ->
    # error < 0 -> heat commanded BELOW the 50% handoff level.
    output = controller.compute(measured_ror_c_per_min=14.0, dt_seconds=5.0)
    assert output.error_c_per_min < 0.0
    assert output.heat_percent < 50


# ---------------------------------------------------------------------------
# Deadband
# ---------------------------------------------------------------------------


def test_deadband_holds_output_and_does_not_move_integrator() -> None:
    config = _config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=8.0,  # flat setpoint for this test
        ror_deadband_c_per_min=1.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=8.0)

    # 8.5 is within +/-1.0 of the flat setpoint 8.0 -> inside the deadband.
    output = controller.compute(measured_ror_c_per_min=8.5, dt_seconds=5.0)
    assert output.heat_percent == 60
    assert output.integrator == pytest.approx(60.0 / config.ki_percent_per_ror_second)

    # A second in-deadband tick must not move the integrator further either.
    output2 = controller.compute(measured_ror_c_per_min=7.6, dt_seconds=5.0)
    assert output2.heat_percent == 60
    assert output2.integrator == pytest.approx(output.integrator)


def test_deadband_boundary_is_inclusive() -> None:
    """``abs(error) <= deadband`` — exactly at the boundary still holds."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=8.0, ror_deadband_c_per_min=1.0
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=55, ror_at_engagement_c_per_min=8.0)

    output = controller.compute(measured_ror_c_per_min=9.0, dt_seconds=5.0)  # error == -1.0
    assert output.heat_percent == 55


# ---------------------------------------------------------------------------
# Floor / ceiling clamp (never 0, and never above heat-at-engagement)
# ---------------------------------------------------------------------------


def test_huge_positive_error_never_exceeds_effective_ceiling() -> None:
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=90, ror_at_engagement_c_per_min=6.1)  # ceiling -> 90

    # RoR near 0 vs the taper's setpoint -> a large positive error every tick.
    output = None
    for _ in range(50):
        output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert output is not None
    assert output.heat_percent <= 90
    assert output.heat_percent == 90
    assert output.saturated is True


def test_huge_negative_error_never_drops_below_floor_and_never_zero() -> None:
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=90, ror_at_engagement_c_per_min=6.1)

    # A very hot RoR relative to the taper's setpoint -> a large negative error.
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
    """A long run with RoR far below the taper's setpoint pins heat at the
    effective ceiling; the integrator must not grow without bound
    (conditional-integration anti-windup rolls back the tentative
    accumulation while saturated)."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=90, ror_at_engagement_c_per_min=6.1)

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
    assert last_output.heat_percent == 90  # sanity: still saturated at the effective ceiling


def test_recovers_promptly_after_saturation_when_ror_crosses_back() -> None:
    """After a long low-RoR saturation run, RoR crossing back above the
    setpoint must leave the ceiling promptly — no long windup lag."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100, ror_smoothing_alpha=1.0)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=90, ror_at_engagement_c_per_min=6.1)

    last_output: PostFcControlOutput | None = None
    for _ in range(100):
        last_output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert last_output is not None
    assert last_output.heat_percent == 90
    assert last_output.saturated is True

    # RoR now well ABOVE the (fully-decayed) setpoint -> heat should drop off
    # the ceiling within a single tick (no backlog of accumulated integrator
    # to unwind first).
    output = controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    assert output.heat_percent < 90


def test_anti_windup_bounded_integrator_compared_to_naive_accumulation() -> None:
    """Without anti-windup a plain integrator accumulating
    ``error * dt_seconds`` every tick over 200 ticks at dt=5s, with a taper
    setpoint capped at ``taper_start_max_ror_c_per_min`` (8.0 — the largest
    the error's RoR-vs-setpoint gap can ever be under this config), would
    reach at most ~8000; the anti-windup-bounded integrator must stay far
    below that (it freezes once saturated)."""
    config = _config(heat_floor_percent=25, heat_ceiling_percent=100)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=90, ror_at_engagement_c_per_min=6.1)

    naive_unbounded_integrator_estimate = 8.0 * 5.0 * 200  # max error(8.0) * dt * ticks
    output = None
    for _ in range(200):
        output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
    assert output is not None
    assert output.integrator < naive_unbounded_integrator_estimate / 4


# ---------------------------------------------------------------------------
# Bumpless reset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("initial_heat", [25, 40, 55, 70, 100])
def test_bumpless_reset_holds_handoff_heat_at_zero_error(initial_heat: int) -> None:
    """Bumpless transfer holds exactly when r0 == the measured engagement
    RoR (the usual case) — here the engagement RoR (8.0) is fed back
    unchanged as the compute sample, so error is zero at tick 1."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=4.0, ror_smoothing_alpha=1.0
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=initial_heat, ror_at_engagement_c_per_min=8.0)

    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=0.001)
    assert output.heat_percent == pytest.approx(initial_heat, abs=1)


def test_bumpless_reset_with_ki_zero_degenerate_path() -> None:
    """When ``ki == 0`` the loop is pure-P; :meth:`reset` stores the initial
    heat directly so the very first zero-error compute still reproduces it
    (see the ``ki == 0`` docstring note in ``PostFcRorController.reset``)."""
    config = _config(
        ki_percent_per_ror_second=0.0,
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=8.0)

    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=0.001)
    assert output.heat_percent == 60


def test_bumpless_reset_clears_ema() -> None:
    """A stale RoR EMA from a previous engagement must not leak across a reset
    (the first post-reset sample becomes the new EMA baseline unblended)."""
    config = _config(ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.1)
    controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)  # pollute the EMA

    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)
    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=0.001)
    # If the stale EMA (blended toward 20.0) had leaked, the smoothed RoR would
    # not equal the fresh sample exactly.
    assert output.smoothed_ror_c_per_min == 8.0


def test_bumpless_reset_not_exact_when_r0_is_clamped_away_from_measured() -> None:
    """Documented non-bumpless edge (B1): when the engagement RoR is
    degenerate and r0 is clamped away from it, tick-1's zero-error assumption
    does not hold — the loop takes a deliberate gentle correction instead of
    reproducing the handoff heat exactly. Tick 1 feeds a RoR well above r0
    (4.0) -> a NEGATIVE error, which moves heat DOWN from the 72% handoff —
    unconstrained by the never-add-heat-beyond-entry ceiling (which only ever
    binds an INCREASE), so the "not exact" deviation is visible directly
    rather than masked by the ceiling clamp (see the dedicated B1/B2 tests
    for the ceiling-interaction cases)."""
    config = _config(
        taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=4.0, ror_smoothing_alpha=1.0
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=72, ror_at_engagement_c_per_min=-3.0)  # r0 clamps to 4.0

    # A RoR well above r0 (4.0): error = 4.0 - 10.0 = -6.0, not zero, so heat
    # moves DOWN from the 72 handoff (a gentle correction, not an exact
    # reproduction) -- proving bumpless transfer does NOT hold when r0 was
    # clamped away from the measured engagement RoR.
    output = controller.compute(measured_ror_c_per_min=10.0, dt_seconds=5.0)
    assert output.error_c_per_min == pytest.approx(-6.0)
    assert output.heat_percent < 72


# ---------------------------------------------------------------------------
# EMA smoothing
# ---------------------------------------------------------------------------


def test_ema_smoothing_dampens_a_single_ror_spike_vs_no_smoothing() -> None:
    smoothed_config = _config(
        ror_smoothing_alpha=0.4, taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=8.0
    )
    raw_config = _config(
        ror_smoothing_alpha=1.0, taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=8.0
    )

    smoothed_controller = PostFcRorController(smoothed_config)
    raw_controller = PostFcRorController(raw_config)
    smoothed_controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)
    raw_controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)

    # Settle both at the (flat) setpoint RoR first so the EMA baseline equals it.
    smoothed_controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    raw_controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)

    # A single spike far from the setpoint.
    spike_smoothed = smoothed_controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)
    spike_raw = raw_controller.compute(measured_ror_c_per_min=20.0, dt_seconds=5.0)

    assert abs(spike_smoothed.error_c_per_min) < abs(spike_raw.error_c_per_min)
    # The smoothed controller's heat move is therefore smaller in magnitude.
    assert abs(spike_smoothed.heat_percent - 50) < abs(spike_raw.heat_percent - 50)


def test_ema_first_sample_has_no_prior_estimate() -> None:
    config = _config(ror_smoothing_alpha=0.4)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.1)

    output = controller.compute(measured_ror_c_per_min=12.0, dt_seconds=5.0)
    assert output.smoothed_ror_c_per_min == 12.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_outputs() -> None:
    config = _config()

    def _run() -> list[PostFcControlOutput]:
        controller = PostFcRorController(config)
        controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.1)
        inputs = [(6.0, 5.0), (7.5, 5.0), (9.0, 5.0), (12.0, 4.0), (3.0, 6.0), (8.0, 5.0)]
        return [controller.compute(measured_ror_c_per_min=ror, dt_seconds=dt) for ror, dt in inputs]

    first_run = _run()
    second_run = _run()
    assert first_run == second_run


# ---------------------------------------------------------------------------
# Output invariants
# ---------------------------------------------------------------------------


def test_output_heat_percent_always_within_effective_box() -> None:
    config = _config(heat_floor_percent=30, heat_ceiling_percent=90)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=6.1)  # ceiling -> 50

    sequence = [0.0, 2.0, 30.0, 8.0, 8.0, 100.0, -5.0, 8.5]
    for ror in sequence:
        output = controller.compute(measured_ror_c_per_min=ror, dt_seconds=5.0)
        assert 30 <= output.heat_percent <= 50  # never above heat-at-engagement (50)
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
    normal wide-box post-FC transient, reproduced deterministically here.

    Under D88 the effective floor/ceiling COLLAPSE TOGETHER with a low
    heat-at-engagement (:meth:`_effective_floor_percent`), so a fresh bumpless
    reset can no longer seed "handoff heat below a WIDER static floor" — that
    combination is now structurally impossible by construction (the box
    collapses down to match). This test instead reproduces the "saturated
    below the (collapsed) floor, still climbing, do not roll back" case
    directly via ``restore_state`` with a hand-built low integrator, proving
    the anti-windup branch itself is unchanged and still reachable regardless
    of how the box got its current bounds.
    """
    config = _config(
        heat_floor_percent=30,
        heat_ceiling_percent=100,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=8.0,  # flat setpoint 8.0 (defaults' deadband is 1.0)
    )
    controller = PostFcRorController(config)
    # A wide-box handoff (heat 50, well above the 30 floor) so the effective
    # box stays [30, 50] uncollapsed, then hand-seed a low integrator (as if
    # several prior ticks had wound it down) via restore_state.
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)
    low_seed = controller.snapshot_state()
    low_seed = dataclasses.replace(
        low_seed,
        integrator=50.0,  # ki*50 = 5, well below the 30 floor at zero error
    )
    controller.restore_state(low_seed)

    # error = +2 (RoR below setpoint 8.0); tentative integrator 50 -> 60;
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
    config = _config(
        taper_start_max_ror_c_per_min=8.0, taper_end_ror_c_per_min=8.0
    )  # floor 25 / ceiling 100, flat setpoint 8.0
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=50, ror_at_engagement_c_per_min=8.0)

    # At exactly the (flat) setpoint the error is within the deadband; output
    # holds at the seeded handoff level (ki*integrator = 0.1 * 500 = 50), in-box.
    output = controller.compute(measured_ror_c_per_min=8.0, dt_seconds=5.0)
    assert output.saturated is False
    assert output.heat_percent == 50


# ---------------------------------------------------------------------------
# D96 (#559): bounded-bidirectional heat recovery — config
# ---------------------------------------------------------------------------


def test_recovery_disabled_by_default() -> None:
    """Hardware-gated promotion posture (identical to D88's own flags before
    their validation roast): recovery is OFF until an operator consciously
    flips it."""
    config = PostFirstCrackControl()
    assert config.recovery_enabled is False


def test_recovery_config_defaults() -> None:
    config = PostFirstCrackControl()
    assert config.recovery_trigger_margin_c_per_min == 1.0
    assert config.recovery_exit_margin_c_per_min == 0.5
    assert config.recovery_confirm_ticks == 3
    assert config.recovery_headroom_percentage_points == 15
    assert config.recovery_exit_glide_pp_per_tick == 5


def test_recovery_exit_margin_must_be_strictly_less_than_trigger_margin() -> None:
    """An equal (or wider) exit margin is limit-cycle-prone (D96 safety
    review): RoR sitting near a SHARED threshold could cross back and forth
    on ordinary tick noise, re-triggering entry immediately after an exit."""
    with pytest.raises(ValueError, match="recovery_exit_margin_c_per_min"):
        PostFirstCrackControl(
            recovery_exit_margin_c_per_min=1.0, recovery_trigger_margin_c_per_min=1.0
        )
    with pytest.raises(ValueError, match="recovery_exit_margin_c_per_min"):
        PostFirstCrackControl(
            recovery_exit_margin_c_per_min=1.5, recovery_trigger_margin_c_per_min=1.0
        )
    # Strictly less is fine (the default relationship).
    PostFirstCrackControl(recovery_exit_margin_c_per_min=0.5, recovery_trigger_margin_c_per_min=1.0)


def test_recovery_enabled_requires_ceiling_guard_drop_enabled() -> None:
    """The blocker finding from the D96 safety review: ``evaluate_command``
    (the gate every heat write goes through) is temperature-blind, so a law
    that can raise heat above entry with the ceiling guard OFF would leave
    the 196 °C bitter line owned solely by the advisor's own judgment."""
    with pytest.raises(ValueError, match="ceiling_guard_drop_enabled"):
        PostFirstCrackControl(recovery_enabled=True, ceiling_guard_drop_enabled=False)
    # The RoR-taper law alone (recovery off) carries no such requirement --
    # it can only ever LOWER the ceiling relative to entry.
    PostFirstCrackControl(enabled=True, recovery_enabled=False, ceiling_guard_drop_enabled=False)
    # Both on together is fine (the only way to construct recovery_enabled=True).
    PostFirstCrackControl(recovery_enabled=True, ceiling_guard_drop_enabled=True)


def test_recovery_enabled_requires_the_ror_taper_master_flag() -> None:
    """PR #560 round 4 Codex finding (P2): ``recovery_enabled=True`` with
    the RoR-taper master flag (``enabled``) OFF is a mislabeling hazard --
    ``_apply_deterministic_post_fc_levers`` gates on ``config.enabled``
    FIRST, so recovery is completely inert in that combination, yet the CLI
    launch banner would print "ENABLED" for a mechanism that never runs.
    The validator's error message names the master flag explicitly (not
    just "enabled", to disambiguate from ``ceiling_guard_drop_enabled``)."""
    with pytest.raises(ValueError, match="enabled=True \\(the RoR-taper master flag\\)"):
        PostFirstCrackControl(recovery_enabled=True, ceiling_guard_drop_enabled=True, enabled=False)
    # The legal combo (all three prerequisites satisfied) still constructs.
    PostFirstCrackControl(recovery_enabled=True, ceiling_guard_drop_enabled=True, enabled=True)


# ---------------------------------------------------------------------------
# D96 (#559): bounded-bidirectional heat recovery — the algorithm
# ---------------------------------------------------------------------------


def _recovery_config(**overrides: object) -> PostFirstCrackControl:
    """A D88-default-shaped config with recovery ENABLED, for the D96 tests."""
    return _config(recovery_enabled=True, ceiling_guard_drop_enabled=True, **overrides)


def test_roast15_recovery_raises_above_entry_bounded() -> None:
    """Replay roast 15's actual measured development trace (run ``8ac8a5e4``,
    store-verified) through the recovery law: heat entered DEVELOPMENT at
    60 % and measured RoR crashed 7.0 -> ~3.0-4.0 °C/min as the advisor pushed
    fan 30 -> 90 for temperature control, leaving the D88-only loop with ZERO
    raise authority (the roast-15 failure this law exists to fix). Under D96,
    heat MUST rise above 60 once the sustained shortfall is confirmed, and
    must NEVER exceed ``60 + recovery_headroom_percentage_points`` (75 at the
    default headroom) -- the hard, error-independent cap holds regardless of
    how far below setpoint RoR falls."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=7.0)

    # The actual store-verified roast-15 RoR sequence (run 8ac8a5e4,
    # telemetry ticks 1053/1062/1070/1078/1086/1094/1102/1110/1118/1126/1134,
    # ~7.7-8.2s apart -- the 5s-cadenced control loop observes this as
    # successive ~5-8s dt_seconds ticks).
    rors = [7.0, 6.1, 6.0, 5.0, 4.07, 5.0, 3.01, 4.0, 3.05, 3.05, 3.05]
    outputs = [controller.compute(measured_ror_c_per_min=r, dt_seconds=7.5) for r in rors]

    heats = [o.heat_percent for o in outputs]
    ceilings = [o.effective_ceiling_percent for o in outputs]
    recovery_flags = [o.recovery_active for o in outputs]

    # The hard cap: heat and the ceiling NEVER exceed 60 + 15 = 75, no matter
    # how far the RoR shortfall grows.
    assert max(heats) <= 75, heats
    assert max(ceilings) <= 75, ceilings
    # Recovery DOES engage on this trace (the whole point of the law) --
    # heat genuinely rises above the 60 % entry value at some point.
    assert any(rf for rf in recovery_flags), recovery_flags
    assert max(heats) > 60, heats


def test_roast15_recovery_never_exceeds_cap_under_extended_shortfall() -> None:
    """Extend roast 15's crash indefinitely (RoR pinned at the crashed value
    well past where the real roast's trace ends) -- the cap must hold no
    matter how long the shortfall persists, not just across the ~11 ticks the
    real trace happens to cover."""
    config = _recovery_config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=7.0)

    heats: list[int] = []
    for _ in range(100):
        output = controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
        heats.append(output.heat_percent)

    assert max(heats) <= 75, heats


def test_roast12_stays_no_raise_under_recovery_law() -> None:
    """Replay roast 12's actual measured development trace (run
    ``edbe9a76``, the validated 9/10 cup, store-verified) through the SAME
    recovery-enabled config: RoR sits at 5.0-7.0 °C/min against a setpoint
    starting at 7.0 and decaying toward 4.0 -- the shortfall never exceeds
    the 1.0 °C/min trigger margin for even a single tick on this trace, so
    recovery must NEVER activate and heat must NEVER rise above the 65 %
    engagement value. This is D88's validated no-op case, now proven to stay
    a no-op under the NEW code path, not just the old one."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=90.0,
        kp_percent_per_ror=3.0,
        ki_percent_per_ror_second=0.1,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=65, ror_at_engagement_c_per_min=7.0)

    # The actual store-verified roast-12 RoR sequence (run edbe9a76,
    # telemetry ticks 906/914/922/929/937/945/952/960/967, ~7-8s apart).
    rors = [7.0, 6.1, 6.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.11]
    outputs = [controller.compute(measured_ror_c_per_min=r, dt_seconds=7.5) for r in rors]

    heats = [o.heat_percent for o in outputs]
    recovery_flags = [o.recovery_active for o in outputs]

    assert max(heats) <= 65, heats
    assert all(rf is False for rf in recovery_flags), recovery_flags


def test_recovery_rollback_discipline_ticks_do_not_fake_accumulate() -> None:
    """A REJECTed/rate-limited tick never even calls ``compute`` in the real
    controller wiring, but this test proves the SNAPSHOT/RESTORE discipline
    (#412 told==enforced, extended to D96's counters) directly: a tentative
    ``compute`` call that gets rolled back via ``restore_state`` must not
    leave its recovery counter increment behind -- the next real tick must
    see the SAME counter value as if the rejected tick had never called
    ``compute`` at all."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=8.0,
        taper_end_ror_c_per_min=4.0,
        taper_duration_seconds=9000.0,  # near-static setpoint for this test
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    # Two real accepted ticks with a sustained shortfall (error > 1.0 margin).
    controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
    controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
    snapshot_before_third = controller.snapshot_state()
    assert snapshot_before_third.recovery_ticks_above_trigger == 2

    # A tentative THIRD tick that gets REJECTed (e.g. rate-limited) -- the
    # real controller wiring snapshots before compute and restores on
    # rejection; simulate that here directly.
    pre_step = controller.snapshot_state()
    controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
    controller.restore_state(pre_step)

    # The counter must be EXACTLY what it was before the rejected tick (2),
    # not 3 -- the rejected tick's tentative increment must not survive.
    # Robustness note (D96 diff safety review): round-trip ALL FOUR recovery
    # fields, not just the entry counter -- the rejected tick's tentative
    # step could in principle also have touched recovery_active or the
    # exit-side state if a future change to the ordering inside
    # _advance_recovery_state ever let entry and exit interact within one
    # call; asserting all four here catches that class directly rather than
    # only the one field this specific scenario happens to move.
    after_restore = controller.snapshot_state()
    assert after_restore == pre_step
    assert after_restore.recovery_ticks_above_trigger == 2
    assert after_restore.recovery_ticks_within_exit == pre_step.recovery_ticks_within_exit
    assert after_restore.recovery_active == pre_step.recovery_active
    assert after_restore.recovery_ticks_since_exit == pre_step.recovery_ticks_since_exit

    # The real next accepted tick (the third genuine one) still correctly
    # confirms entry on ITS OWN third consecutive tick, not a phantom fourth.
    third = controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
    assert third.recovery_active is True


def test_reset_clears_recovery_state_from_active_and_mid_glide() -> None:
    """D96 diff safety review finding (the surviving mutant): ``reset``'s
    four recovery-field zeroing assignments are otherwise exercised but never
    directly ASSERTED -- every other recovery test calls ``reset`` exactly
    once on a FRESH controller, where ``__init__`` already zeroed the fields,
    so deleting the zeroing lines in ``reset`` itself passed every existing
    test. This test drives recovery into two DIFFERENT non-fresh states on
    the SAME controller instance, then calls ``reset`` again (a fresh
    true-FC-edge engagement, mirroring
    ``test_c2_fresh_engage_recaptures_and_does_not_inherit_prior_episode``'s
    taper-state precedent) and asserts the recovery state is fully cleared
    each time -- the documented invariant (no recovery-state leak from a
    PRIOR engagement into a new one) is unreachable in today's per-run
    controller construction (a fresh ``RoastController`` per run, api.py) but
    is one refactor away, so it is pinned here regardless.

    Fail-then-pass: re-deleting the four zeroing assignments in ``reset``
    (``post_fc_control.py`` — the lines setting
    ``recovery_ticks_above_trigger``, ``recovery_ticks_within_exit``,
    ``recovery_active``, and ``recovery_ticks_since_exit`` to their cleared
    values) must fail this test."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,  # static setpoint at 6.0
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)

    # --- Case 1: reset from a FULLY ACTIVE recovery state. ---
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    for _ in range(3):
        controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)  # confirm entry
    active_snapshot = controller.snapshot_state()
    assert active_snapshot.recovery_active is True  # precondition: genuinely active
    controller.restore_state(
        dataclasses.replace(
            active_snapshot,
            recovery_trigger=PostFcRecoveryTrigger.PROJECTION,
            recovery_projection_short_ticks=2,
            recovery_projection_on_target_ticks=1,
            recovery_projection_release_latched=True,
            recovery_last_development_elapsed_seconds=60.0,
            recovery_last_charge_elapsed_seconds=500.0,
            recovery_cutoff_reached=True,
        )
    )

    controller.reset(initial_heat_percent=55, ror_at_engagement_c_per_min=6.0)
    cleared_from_active = controller.snapshot_state()
    assert cleared_from_active.recovery_ticks_above_trigger == 0
    assert cleared_from_active.recovery_ticks_within_exit == 0
    assert cleared_from_active.recovery_active is False
    assert cleared_from_active.recovery_ticks_since_exit is None
    assert cleared_from_active.recovery_trigger is PostFcRecoveryTrigger.NONE
    assert cleared_from_active.recovery_projection_short_ticks == 0
    assert cleared_from_active.recovery_projection_on_target_ticks == 0
    assert cleared_from_active.recovery_projection_release_latched is False
    assert cleared_from_active.recovery_last_development_elapsed_seconds is None
    assert cleared_from_active.recovery_last_charge_elapsed_seconds is None
    assert cleared_from_active.recovery_cutoff_reached is False
    # The first post-reset tick computes the D88 BASE ceiling for the NEW
    # engagement heat (55), never a leaked recovery cap from the old one.
    first_tick = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert first_tick.effective_ceiling_percent == 55
    assert first_tick.recovery_active is False

    # --- Case 2: reset from MID-GLIDE (recovery inactive but not yet fully
    # settled back to the base -- a DIFFERENT non-fresh state than Case 1,
    # since ``recovery_ticks_since_exit`` is the field Case 1 never
    # exercises a non-None value for). ---
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    for _ in range(3):
        controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)  # confirm entry
    for _ in range(3):
        controller.compute(measured_ror_c_per_min=5.6, dt_seconds=5.0)  # confirm exit
    mid_glide = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert 60 < mid_glide.effective_ceiling_percent < 75  # genuinely mid-glide
    mid_glide_snapshot = controller.snapshot_state()
    assert mid_glide_snapshot.recovery_ticks_since_exit is not None  # precondition

    controller.reset(initial_heat_percent=45, ror_at_engagement_c_per_min=6.0)
    cleared_from_glide = controller.snapshot_state()
    assert cleared_from_glide.recovery_ticks_above_trigger == 0
    assert cleared_from_glide.recovery_ticks_within_exit == 0
    assert cleared_from_glide.recovery_active is False
    assert cleared_from_glide.recovery_ticks_since_exit is None
    second_tick = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert second_tick.effective_ceiling_percent == 45  # the NEW engagement's D88 base
    assert second_tick.recovery_active is False


def test_zero_headroom_recovering_reports_holding_not_a_phantom_elevation() -> None:
    """PR #560 round 3 Codex finding: with zero headroom
    (``recovery_headroom_percentage_points=0``), the entry condition can
    still CONFIRM (measured RoR persistently below setpoint, the trigger
    margin exceeded for ``recovery_confirm_ticks`` consecutive ticks) — but
    ``_recovery_ceiling_percent()`` equals the D88 base exactly
    (``min(heat_ceiling_percent, heat_engage_percent + 0) ==
    heat_engage_percent`` whenever ``heat_engage_percent <=
    heat_ceiling_percent``, always true), so NOTHING actually elevates. The
    reported state must stay ``HOLDING`` (not the internal counters'
    ``RECOVERING``) throughout, the ceiling must stay pinned at the D88 base
    the entire time, and ``recovery_active`` must stay ``False`` — a
    controller reading this state to decide whether to suppress a drop-tick
    write must never suppress one for a "recovery" that never actually
    raised anything."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
        recovery_headroom_percentage_points=0,
        recovery_confirm_ticks=1,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    # A sustained shortfall well past the trigger margin -- the entry
    # condition genuinely confirms every tick.
    outputs = [controller.compute(measured_ror_c_per_min=4.0, dt_seconds=5.0) for _ in range(10)]
    assert all(o.heat_authority_state is PostFcHeatAuthorityState.HOLDING for o in outputs), [
        o.heat_authority_state for o in outputs
    ]
    assert all(o.recovery_active is False for o in outputs)
    assert all(o.effective_ceiling_percent == 60 for o in outputs), [
        o.effective_ceiling_percent for o in outputs
    ]


def test_entry_at_heat_ceiling_with_headroom_reports_holding_not_recovering() -> None:
    """PR #560 round 3 Codex finding, the second reachable path to the same
    bug: entry heat ALREADY AT ``heat_ceiling_percent`` (100 here) means
    ``_recovery_ceiling_percent()`` (``min(100, 100 + 15) == 100``) equals
    the D88 base regardless of a NON-zero headroom config -- there is simply
    no room ABOVE the static ceiling to raise into. Same assertions as the
    zero-headroom case: the reported state must stay ``HOLDING`` throughout,
    never a phantom ``RECOVERING``."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
        recovery_headroom_percentage_points=15,
        recovery_confirm_ticks=1,
        heat_ceiling_percent=100,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=100, ror_at_engagement_c_per_min=6.0)

    outputs = [controller.compute(measured_ror_c_per_min=4.0, dt_seconds=5.0) for _ in range(10)]
    assert all(o.heat_authority_state is PostFcHeatAuthorityState.HOLDING for o in outputs), [
        o.heat_authority_state for o in outputs
    ]
    assert all(o.recovery_active is False for o in outputs)
    assert all(o.effective_ceiling_percent == 100 for o in outputs), [
        o.effective_ceiling_percent for o in outputs
    ]


def test_recovery_fuzz_ror_pinned_at_zero_saturates_at_cap_500_ticks() -> None:
    """Fuzz variant 1 (mandatory): pin measured RoR at 0.0 -- an even more
    extreme, sustained-forever shortfall than any real roast trace -- for
    500+ ticks. Heat must saturate at (never exceed)
    ``min(heat_ceiling_percent, heat_engage_percent +
    recovery_headroom_percentage_points)`` even under a pathological,
    indefinitely-sustained worst-case error. This is the closest replicable
    analogue to what killed roast 9 (an unbounded-forever error against an
    unbounded-upward ceiling) -- proving the cap holds even there is the
    direct test of the module docstring's structural-impossibility claim."""
    config = _recovery_config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    heats: list[int] = []
    for _ in range(500):
        output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0)
        heats.append(output.heat_percent)

    expected_cap = min(config.heat_ceiling_percent, 60 + config.recovery_headroom_percentage_points)
    assert max(heats) <= expected_cap, (max(heats), expected_cap)
    # It genuinely saturates AT the cap (not just under it) given long enough.
    assert heats[-1] == expected_cap


def test_recovery_fuzz_oscillating_ror_limit_cycle_stays_bounded() -> None:
    """Fuzz variant 2 (mandatory): an RoR trace oscillating periodically
    across BOTH the entry and exit thresholds (a synthetic worst case for the
    limit-cycle risk the D96 safety review raised) must never cause the
    ceiling to sawtooth UNBOUNDEDLY -- entries and exits may recur (a bounded,
    periodic sawtooth between the D88 base and the recovery cap is an
    accepted, designed-for outcome), but the ceiling must never exceed the
    recovery cap nor fall below the D88 base ceiling, and the number of
    state-transitions over a long run must stay proportional to the number of
    oscillation cycles (not accelerating/diverging)."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,  # static setpoint at 6.0 throughout
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    base_ceiling = 60
    recovery_cap = 60 + config.recovery_headroom_percentage_points  # 75

    # RoR oscillates: 3 ticks at 4.9 (error 1.1 > trigger 1.0 -- shortfall),
    # then 3 ticks at 5.6 (error 0.4 <= exit margin 0.5 -- recovered) --
    # crosses both thresholds every 6-tick cycle, the sharpest oscillation
    # this hysteresis gap is designed to survive.
    cycle = [4.9, 4.9, 4.9, 5.6, 5.6, 5.6]
    rors = cycle * 30  # 180 ticks, 30 full cycles

    ceilings: list[int] = []
    actives: list[bool] = []
    for r in rors:
        output = controller.compute(measured_ror_c_per_min=r, dt_seconds=5.0)
        ceilings.append(output.effective_ceiling_percent)
        actives.append(output.recovery_active)

    # Bounded: never above the recovery cap, never below the D88 base.
    assert max(ceilings) <= recovery_cap, ceilings
    assert min(ceilings) >= base_ceiling, ceilings

    # State transitions stay proportional to the 30 forcing cycles -- NOT
    # accelerating/diverging (a limit cycle "running away" would show far
    # more transitions than forcing cycles, or a ceiling that never returns
    # to the base). Count True->False and False->True edges.
    edges = sum(1 for prev, cur in zip([False, *actives], actives, strict=False) if prev != cur)
    assert edges <= 2 * len(rors) // 6 + 2, (edges, len(rors))
    # And it genuinely DOES return to the D88 base ceiling repeatedly (not
    # stuck at the recovery cap forever) -- the exit half of the law works.
    assert base_ceiling in ceilings


def test_recovery_exit_glides_down_not_snaps() -> None:
    """The exit glide directly: once recovery exits, the ceiling must
    descend by at most ``recovery_exit_glide_pp_per_tick`` per tick toward
    ``heat_engage_percent``, never snap back in one step -- the direct guard
    against a raise->recover->snap-cut->crash->re-trigger limit cycle."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,  # static setpoint at 6.0
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
        recovery_exit_glide_pp_per_tick=5,
        recovery_confirm_ticks=3,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    # Confirm entry: 3 ticks with error 1.1 > trigger margin 1.0.
    entry_outputs = [
        controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0) for _ in range(3)
    ]
    entry_output = entry_outputs[-1]
    assert entry_output.heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert entry_output.recovery_active is True
    assert entry_output.effective_ceiling_percent == 75  # jumps immediately, no glide on entry

    # Confirm exit: 3 ticks with error 0.4 <= exit margin 0.5.
    exit_outputs = [
        controller.compute(measured_ror_c_per_min=5.6, dt_seconds=5.0) for _ in range(3)
    ]
    ceilings_during_exit = [o.effective_ceiling_percent for o in exit_outputs]
    # PR #560 Codex finding (diagnostics gap): the ceiling is STILL well
    # above the D88 base right after exit confirms -- heat_authority_state
    # correctly reads GLIDING (not HOLDING), and the derived recovery_active
    # boolean (True for BOTH RECOVERING and GLIDING) reflects that too. The
    # raw internal _recovery_active flag flips False here, but neither
    # public field does.
    assert exit_outputs[-1].heat_authority_state is PostFcHeatAuthorityState.GLIDING
    assert exit_outputs[-1].recovery_active is True

    # The tick exit is CONFIRMED on already reflects one glide step down
    # from 75 (per the docstring: ticks_since_exit starts at 1 on the
    # confirming tick itself, so the glide begins on the same read).
    assert ceilings_during_exit[-1] == 70  # 75 - 1*5

    # Subsequent ticks continue gliding down by at most 5pp/tick until the
    # D88 base (60) is reached and held there.
    post_exit_outputs = [
        controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0) for _ in range(5)
    ]
    post_exit_ceilings = [o.effective_ceiling_percent for o in post_exit_outputs]
    assert post_exit_ceilings == [65, 60, 60, 60, 60]
    # heat_authority_state tracks the glide precisely: GLIDING while above
    # the base, HOLDING once it locks there -- never HOLDING while elevated.
    assert [o.heat_authority_state for o in post_exit_outputs] == [
        PostFcHeatAuthorityState.GLIDING,
        PostFcHeatAuthorityState.HOLDING,
        PostFcHeatAuthorityState.HOLDING,
        PostFcHeatAuthorityState.HOLDING,
        PostFcHeatAuthorityState.HOLDING,
    ]
    assert [o.recovery_active for o in post_exit_outputs] == [True, False, False, False, False]
    # No step in the entire glide ever exceeds the configured per-tick rate.
    full_sequence = [75, *ceilings_during_exit, *post_exit_ceilings]
    steps = [a - b for a, b in zip(full_sequence, full_sequence[1:], strict=False)]
    assert all(step <= config.recovery_exit_glide_pp_per_tick for step in steps), steps


def test_recovery_re_entry_during_glide_cancels_the_glide_immediately() -> None:
    """If RoR crashes again WHILE the ceiling is still gliding down from a
    prior exit, re-entry must engage from the CURRENT (partially-glided)
    ceiling state and jump straight back to the full recovery cap -- the
    glide-state must not persist or interfere with a fresh entry.

    Uses a wider headroom (20, vs the 15 default) and a shorter confirm bar
    (2 ticks) so the re-crash's SECOND confirm-tick lands WHILE the glide
    counter is still active (not yet settled to HOLDING) -- exercising the
    re-entry branch that fires from mid-glide specifically (as opposed to a
    re-entry that happens to land after the glide has already settled back
    to the D88 base, a structurally different code path this test does NOT
    cover on its own)."""
    config = _recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,
        ki_percent_per_ror_second=0.0,
        ror_smoothing_alpha=1.0,
        recovery_headroom_percentage_points=20,
        recovery_confirm_ticks=2,
    )
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    entry_check = controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)
    assert entry_check.heat_authority_state is PostFcHeatAuthorityState.HOLDING  # tick 1 of 2
    entry_confirm = controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)  # tick 2 of 2
    assert entry_confirm.effective_ceiling_percent == 80  # 60 + 20

    exit_check = controller.compute(measured_ror_c_per_min=5.6, dt_seconds=5.0)
    assert exit_check.heat_authority_state is PostFcHeatAuthorityState.RECOVERING  # tick 1 of 2
    exit_confirm = controller.compute(measured_ror_c_per_min=5.6, dt_seconds=5.0)  # tick 2 of 2
    # PR #560 Codex finding: right at exit confirmation the ceiling is still
    # gliding down from the recovery cap -- GLIDING, not HOLDING, and
    # recovery_active (True for both non-holding states) stays True too.
    assert exit_confirm.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    assert exit_confirm.recovery_active is True
    mid_glide = controller.compute(measured_ror_c_per_min=6.0, dt_seconds=5.0)
    assert 60 < mid_glide.effective_ceiling_percent < 80  # genuinely mid-glide
    assert mid_glide.heat_authority_state is PostFcHeatAuthorityState.GLIDING

    # RoR crashes again before the glide reaches the base. The SECOND tick
    # of this pair is the one that confirms re-entry (recovery_confirm_ticks
    # =2) while `_recovery_ticks_since_exit` is still non-None -- the
    # mid-glide re-entry branch, not the settled-to-HOLDING one.
    re_entry_first = controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)
    assert re_entry_first.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    re_entry = controller.compute(measured_ror_c_per_min=4.9, dt_seconds=5.0)
    assert re_entry.heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert re_entry.recovery_active is True
    assert re_entry.effective_ceiling_percent == 80  # full cap, not a partial value


def test_drop_and_estop_precedence_over_recovery_raise() -> None:
    """Documents the structural boundary this module's algorithm sits inside
    (the REAL, mandatory drop/e-stop-precedence assertions are end-to-end
    controller tests: ``test_controller.py::test_estop_precedence_over_
    recovery_raise_same_tick`` and ``::test_ceiling_guard_drop_takes_
    precedence_over_recovery_raise_same_tick``, which drive an actual
    ``RoastController.tick()`` and assert on the real phase/executor
    outcome).

    A raised heat command is only ever considered inside DEVELOPMENT, and
    both the deterministic drop paths (the dev%/temp anchor and the
    ceiling-guard drop) and the hardware e-stop live entirely OUTSIDE this
    module's algorithm, in the controller's tick order:
    ``_evaluate_safety``/``_act_on_safety`` (e-stop) runs BEFORE
    ``_apply_deterministic_post_fc_levers`` (this module's caller), and
    ``_maybe_ceiling_guard_drop`` runs immediately AFTER it in the same tick
    -- so a tick that ALSO qualifies for e-stop or a drop this same tick
    still lands there, never blocked or delayed by a recovery raise. This
    module has no drop or e-stop concept at all: it is a pure heat-percentage
    computation with no MCP/roaster access, so it structurally cannot
    override or race either path -- the precedence lives entirely in
    ``controller.tick()``'s call order."""
    # This module (post_fc_control.py) never imports controller, safety, or
    # mcp_client (see the module docstring) -- there is no drop/e-stop
    # concept to construct or race against here. This test documents (the
    # controller-level tests named above VERIFY end-to-end) that the
    # precedence is structural: nothing in THIS module's public API can
    # express a drop or an e-stop, so recovery cannot delay or block one by
    # construction.
    config = _recovery_config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    output = controller.compute(measured_ror_c_per_min=3.0, dt_seconds=5.0)
    # The output is purely a heat percentage + diagnostics -- no field here
    # can suppress a drop or an e-stop; the caller (controller.py) is
    # unconditionally responsible for running the drop/e-stop checks in its
    # own tick order regardless of this value.
    assert isinstance(output.heat_percent, int)


# ---------------------------------------------------------------------------
# Recovery v2 projection control law (#708 slice 1)
# ---------------------------------------------------------------------------


def test_recovery_projection_defaults_and_cross_field_guards() -> None:
    """The experimental projection knobs are opt-in and bounded at load time."""
    defaults = PostFirstCrackControl()
    assert defaults.recovery_projection_enabled is False
    assert defaults.recovery_projection_entry_horizon_pp == 2.0
    assert defaults.recovery_projection_cutoff_horizon_pp == 5.0
    assert defaults.recovery_projection_margin_c == 3.0
    assert defaults.recovery_entry_step_pp == 10
    with pytest.raises(ValueError, match="requires recovery_enabled"):
        _config(recovery_projection_enabled=True)
    with pytest.raises(ValueError, match="strictly greater"):
        _projection_recovery_config(
            recovery_projection_entry_horizon_pp=5.0,
            recovery_projection_cutoff_horizon_pp=5.0,
        )
    with pytest.raises(ValueError):
        _projection_recovery_config(recovery_projection_entry_horizon_pp=float("nan"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("recovery_projection_entry_horizon_pp", 0.0, "greater than 0"),
        ("recovery_projection_entry_horizon_pp", 20.1, "less than or equal to 20"),
        ("recovery_projection_entry_horizon_pp", float("nan"), "finite number"),
        ("recovery_projection_entry_horizon_pp", float("inf"), "finite number"),
        ("recovery_projection_entry_horizon_pp", float("-inf"), "finite number"),
        ("recovery_projection_cutoff_horizon_pp", 0.0, "greater than 0"),
        ("recovery_projection_cutoff_horizon_pp", 20.1, "less than or equal to 20"),
        ("recovery_projection_cutoff_horizon_pp", float("nan"), "finite number"),
        ("recovery_projection_cutoff_horizon_pp", float("inf"), "finite number"),
        ("recovery_projection_cutoff_horizon_pp", float("-inf"), "finite number"),
        ("recovery_projection_margin_c", 0.0, "greater than 0"),
        ("recovery_projection_margin_c", -1.0, "greater than 0"),
        ("recovery_projection_margin_c", float("nan"), "finite number"),
        ("recovery_projection_margin_c", float("inf"), "finite number"),
        ("recovery_projection_margin_c", float("-inf"), "finite number"),
        ("recovery_entry_step_pp", -1, "greater than or equal to 0"),
        ("recovery_entry_step_pp", 51, "less than or equal to 50"),
    ],
)
def test_recovery_projection_knob_bounds_reject_each_invalid_class(
    field: str,
    value: float | int,
    message: str,
) -> None:
    """Every new tuning bound and finite constraint has a direct guard."""
    with pytest.raises(ValueError, match=message):
        _projection_recovery_config(**{field: value})


@pytest.mark.parametrize("step", [0, 50])
def test_recovery_entry_step_accepts_exact_bounds(step: int) -> None:
    """The ratified inclusive fast-raise bounds remain usable."""
    assert _projection_recovery_config(recovery_entry_step_pp=step).recovery_entry_step_pp == step


def test_projection_entry_needs_three_uninterrupted_short_ticks() -> None:
    """A non-short tick resets projection confirmation before entry."""
    controller = PostFcRorController(
        _projection_recovery_config(recovery_trigger_margin_c_per_min=50.0)
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    for development in (60.0, 65.0):
        output = controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(development_elapsed_seconds=development),
        )
        assert output.recovery_active is False
    controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(bean_temp_c=199.0, development_elapsed_seconds=70.0),
    )
    assert controller.snapshot_state().recovery_projection_short_ticks == 0
    controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=75.0),
    )
    controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=80.0),
    )
    entered = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=85.0),
    )
    assert entered.recovery_trigger is PostFcRecoveryTrigger.PROJECTION
    assert entered.heat_authority_state is PostFcHeatAuthorityState.RECOVERING


def test_projection_does_not_depend_on_taper_setpoint() -> None:
    """Changing taper parameters cannot move an entry driven only by projection."""
    entries: list[int] = []
    for taper_end in (2.0, 5.0):
        controller = PostFcRorController(
            _projection_recovery_config(
                taper_end_ror_c_per_min=taper_end,
                taper_start_max_ror_c_per_min=9.0,
                recovery_trigger_margin_c_per_min=50.0,
            )
        )
        controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
        for tick in range(1, 5):
            output = controller.compute(
                measured_ror_c_per_min=0.0,
                dt_seconds=5.0,
                projection=_projection(development_elapsed_seconds=55.0 + tick * 5.0),
            )
            if output.recovery_trigger is PostFcRecoveryTrigger.PROJECTION:
                entries.append(tick)
                break
    assert entries == [3, 3]


def test_conebosque_shape_enters_at_twelve_percent_and_reaches_cap_boundedly() -> None:
    """Live-cadence projection enters with four DTR points and reaches the cap."""
    config = _projection_recovery_config(
        taper_start_max_ror_c_per_min=6.0,
        taper_end_ror_c_per_min=6.0,
        taper_duration_seconds=1.0,
    )
    v2 = PostFcRorController(config)
    v1 = PostFcRorController(config.model_copy(update={"recovery_projection_enabled": False}))
    for controller in (v2, v1):
        controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    entry_projection: PostFcProjectionInputs | None = None
    for development, charge, ror in (
        (50.0, 490.0, 6.0),
        (55.0, 495.0, 5.5),
        (60.0, 500.0, 4.9),
    ):
        entry_projection = _projection(
            development_elapsed_seconds=development,
            charge_elapsed_seconds=charge,
        )
        entered = v2.compute(
            measured_ror_c_per_min=ror,
            dt_seconds=5.0,
            projection=entry_projection,
        )
        v1_output = v1.compute(measured_ror_c_per_min=ror, dt_seconds=5.0)

    assert entry_projection is not None
    entry_dtr = 100.0 * 60.0 / 500.0
    assert entry_dtr == 12.0
    assert entry_projection.target_development_percent - entry_dtr == 4.0
    assert entered.recovery_trigger is PostFcRecoveryTrigger.PROJECTION
    assert v1_output.recovery_trigger is PostFcRecoveryTrigger.NONE
    cap = min(config.heat_ceiling_percent, 60 + config.recovery_headroom_percentage_points)
    post_entry: list[PostFcControlOutput] = []
    for development, charge in ((65.0, 505.0), (70.0, 510.0)):
        post_entry.append(
            v2.compute(
                measured_ror_c_per_min=0.0,
                dt_seconds=5.0,
                projection=_projection(
                    development_elapsed_seconds=development,
                    charge_elapsed_seconds=charge,
                ),
            )
        )
    assert post_entry[-1].heat_percent == cap


def test_v2_sustained_shortfall_stays_at_cap_for_500_ticks() -> None:
    """The projection path cannot compound beyond the unchanged D96 cap."""
    config = _projection_recovery_config()
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    outputs = [
        controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(),
        )
        for _ in range(500)
    ]
    cap = min(config.heat_ceiling_percent, 60 + config.recovery_headroom_percentage_points)
    assert max(output.heat_percent for output in outputs) == cap
    first_cap_tick = next(
        index for index, output in enumerate(outputs, start=1) if output.heat_percent == cap
    )
    assert first_cap_tick <= 4


@pytest.mark.parametrize("trigger_projection", [False, True])
def test_v2_fast_raise_is_bounded_non_lowering_and_non_compounding(
    trigger_projection: bool,
) -> None:
    """Every v2 entry gets one bounded raise regardless of its trigger."""
    controller = PostFcRorController(
        _projection_recovery_config(
            recovery_confirm_ticks=1,
            recovery_headroom_percentage_points=15,
            recovery_entry_step_pp=10,
        )
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    projection = _projection() if trigger_projection else _projection(bean_temp_c=199.0)
    entered = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=projection)
    assert entered.recovery_trigger is (
        PostFcRecoveryTrigger.PROJECTION if trigger_projection else PostFcRecoveryTrigger.ROR_ERROR
    )
    # The natural PI output is already 73, above the 70-point entry floor.
    # Pinning it proves the floor uses max rather than overwriting a safely
    # higher command with the lower floor target.
    assert entered.heat_percent == 73
    assert 700.0 <= entered.integrator < 1000.0
    assert entered.recovery_fast_raise_applied is True
    next_tick = controller.compute(
        measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=projection
    )
    assert next_tick.recovery_fast_raise_applied is False
    assert next_tick.heat_percent <= 75


def test_v2_fast_raise_reentry_reseeds_without_addition() -> None:
    """A second independent v2 entry reuses, rather than adds, its floor."""
    controller = PostFcRorController(
        _projection_recovery_config(
            recovery_confirm_ticks=1,
            ki_percent_per_ror_second=0.0,
            recovery_entry_step_pp=10,
        )
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    first_entry = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(),
    )
    first_bias = controller.snapshot_state().bias_percent
    assert first_entry.recovery_fast_raise_applied is True
    assert first_entry.heat_percent == 70
    assert first_bias == 70.0

    released = controller.compute(
        measured_ror_c_per_min=20.0,
        dt_seconds=5.0,
        projection=_projection(bean_temp_c=200.0),
    )
    assert released.heat_authority_state is PostFcHeatAuthorityState.GLIDING

    second_entry = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(),
    )
    assert second_entry.recovery_trigger is PostFcRecoveryTrigger.PROJECTION
    assert second_entry.recovery_fast_raise_applied is True
    assert second_entry.heat_percent == 70
    assert controller.snapshot_state().bias_percent == first_bias


def test_projection_input_is_byte_identical_when_projection_flag_is_false() -> None:
    """Supplying ignored v2 inputs cannot perturb the v1 command sequence."""
    config = _recovery_config(ror_smoothing_alpha=1.0)
    with_inputs = PostFcRorController(config)
    without_inputs = PostFcRorController(config)
    for controller in (with_inputs, without_inputs):
        controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    for tick, measured_ror in enumerate((6.0, 4.8, 4.7, 5.8, 6.1), start=1):
        with_output = with_inputs.compute(
            measured_ror_c_per_min=measured_ror,
            dt_seconds=5.0,
            projection=_projection(
                development_elapsed_seconds=50.0 + 5.0 * tick,
                charge_elapsed_seconds=490.0 + 5.0 * tick,
            ),
        )
        without_output = without_inputs.compute(
            measured_ror_c_per_min=measured_ror,
            dt_seconds=5.0,
        )
        assert with_output == without_output
        assert with_inputs.snapshot_state() == without_inputs.snapshot_state()


def test_same_tick_projection_and_ror_confirmation_prefers_projection() -> None:
    """The typed projection trigger wins when both entry paths confirm."""
    config = _projection_recovery_config(recovery_confirm_ticks=1)
    controller = PostFcRorController(config)
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    output = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(),
    )

    assert output.error_c_per_min > config.recovery_trigger_margin_c_per_min
    assert output.projected_entry_temp_c is not None
    assert output.projected_entry_temp_c < 197.0
    assert output.recovery_trigger is PostFcRecoveryTrigger.PROJECTION


def test_projection_on_target_glides_and_cutoff_latches_all_recovery_until_reset() -> None:
    """The +5 horizon releases through glide and prevents every later entry."""
    controller = PostFcRorController(_projection_recovery_config(recovery_confirm_ticks=3))
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    for development in (60.0, 65.0, 70.0):
        controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(development_elapsed_seconds=development),
        )
    output: PostFcControlOutput | None = None
    for tick, development in enumerate((75.0, 80.0, 85.0), start=1):
        output = controller.compute(
            measured_ror_c_per_min=20.0,
            dt_seconds=5.0,
            projection=_projection(bean_temp_c=200.0, development_elapsed_seconds=development),
        )
        if tick < 3:
            assert output.heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert output is not None
    assert output.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    cutoff = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=110.0),
    )
    assert cutoff.recovery_trigger is PostFcRecoveryTrigger.NONE
    assert controller.snapshot_state().recovery_cutoff_reached is True
    after_cutoff_states: list[PostFcHeatAuthorityState] = []
    after_cutoff_ceilings: list[int] = []
    for development in (115.0, 120.0, 125.0):
        later = controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(development_elapsed_seconds=development),
        )
        after_cutoff_states.append(later.heat_authority_state)
        after_cutoff_ceilings.append(later.effective_ceiling_percent)
        assert later.heat_authority_state is not PostFcHeatAuthorityState.RECOVERING
    assert after_cutoff_ceilings == [60, 60, 60]
    assert after_cutoff_states == [
        PostFcHeatAuthorityState.HOLDING,
        PostFcHeatAuthorityState.HOLDING,
        PostFcHeatAuthorityState.HOLDING,
    ]
    missing_projection = [
        controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=None)
        for _ in range(3)
    ]
    assert all(
        output.heat_authority_state is PostFcHeatAuthorityState.HOLDING
        and output.recovery_trigger is PostFcRecoveryTrigger.NONE
        for output in missing_projection
    )
    assert controller.snapshot_state().recovery_ticks_above_trigger == 0
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    assert controller.snapshot_state().recovery_cutoff_reached is False


def test_on_target_cutoff_projection_blocks_overlapping_entry_reconfirmation() -> None:
    """A sufficient +5 projection cannot re-enter on the still-short +2 view."""
    controller = PostFcRorController(
        _projection_recovery_config(
            recovery_confirm_ticks=2,
            recovery_trigger_margin_c_per_min=50.0,
        )
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    entry_outputs = [
        controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(),
        )
        for _ in range(2)
    ]
    assert entry_outputs[-1].heat_authority_state is PostFcHeatAuthorityState.RECOVERING

    # At these exact inputs the +2 projection is still below target-minus-
    # margin, while the later +5 projection already reaches the target.
    overlap = _projection(bean_temp_c=189.5)
    release_outputs = [
        controller.compute(
            measured_ror_c_per_min=12.0,
            dt_seconds=5.0,
            projection=overlap,
        )
        for _ in range(2)
    ]
    for released in release_outputs:
        assert released.projected_entry_temp_c is not None
        assert released.projected_entry_temp_c < 197.0
        assert released.projected_cutoff_temp_c is not None
        assert released.projected_cutoff_temp_c >= 200.0
    assert release_outputs[-1].heat_authority_state is PostFcHeatAuthorityState.GLIDING

    after_release = [
        controller.compute(
            measured_ror_c_per_min=12.0,
            dt_seconds=5.0,
            projection=overlap,
        )
        for _ in range(4)
    ]
    assert all(
        output.heat_authority_state is not PostFcHeatAuthorityState.RECOVERING
        and output.recovery_trigger is PostFcRecoveryTrigger.NONE
        for output in after_release
    )
    assert after_release[-1].heat_authority_state is PostFcHeatAuthorityState.HOLDING
    assert controller.snapshot_state().recovery_projection_release_latched is True

    worsened = [
        controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(),
        )
        for _ in range(2)
    ]
    assert worsened[-1].heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert worsened[-1].recovery_trigger is PostFcRecoveryTrigger.PROJECTION
    assert controller.snapshot_state().recovery_projection_release_latched is False


def test_overlapping_cutoff_projection_does_not_veto_initial_entry() -> None:
    """D162's +2 entry signal remains independent of the later +5 view."""
    controller = PostFcRorController(
        _projection_recovery_config(
            recovery_confirm_ticks=2,
            recovery_trigger_margin_c_per_min=50.0,
        )
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    overlap = _projection(bean_temp_c=189.5)

    outputs = [
        controller.compute(
            measured_ror_c_per_min=12.0,
            dt_seconds=5.0,
            projection=overlap,
        )
        for _ in range(2)
    ]

    assert outputs[-1].projected_entry_temp_c is not None
    assert outputs[-1].projected_entry_temp_c < 197.0
    assert outputs[-1].projected_cutoff_temp_c is not None
    assert outputs[-1].projected_cutoff_temp_c >= 200.0
    assert outputs[-1].heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert outputs[-1].recovery_trigger is PostFcRecoveryTrigger.PROJECTION


@pytest.mark.parametrize(
    "projection",
    [
        None,
        _projection(bean_temp_c=float("nan")),
        _projection(bean_temp_c=float("inf")),
        _projection(bean_temp_c=float("-inf")),
        _projection(target_drop_temp_c=float("nan")),
        _projection(target_drop_temp_c=float("inf")),
        _projection(target_drop_temp_c=float("-inf")),
        _projection(target_development_percent=float("nan")),
        _projection(target_development_percent=float("inf")),
        _projection(target_development_percent=float("-inf")),
        _projection(development_elapsed_seconds=None),
        _projection(development_elapsed_seconds=float("nan")),
        _projection(development_elapsed_seconds=float("inf")),
        _projection(development_elapsed_seconds=float("-inf")),
        _projection(charge_elapsed_seconds=float("nan")),
        _projection(charge_elapsed_seconds=float("inf")),
        _projection(charge_elapsed_seconds=float("-inf")),
        _projection(charge_elapsed_seconds=0.0),
        _projection(development_elapsed_seconds=-1.0),
        _projection(development_elapsed_seconds=501.0),
        _projection(target_development_percent=96.0),
        _projection(target_development_percent=99.0),
    ],
)
def test_invalid_projection_is_inert_and_v1_remains_available(
    projection: PostFcProjectionInputs | None,
) -> None:
    """Bad projection never crosses the control boundary or disables v1."""
    controller = PostFcRorController(_projection_recovery_config(recovery_confirm_ticks=1))
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    output = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=projection)
    assert output.projection_valid is False
    assert output.recovery_trigger is PostFcRecoveryTrigger.ROR_ERROR


def test_invalid_projection_releases_projection_recovery_and_snapshot_restores_all_fields() -> None:
    """Invalid active projection glides out; tentative state remains reversible."""
    controller = PostFcRorController(_projection_recovery_config(recovery_confirm_ticks=1))
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=_projection())
    assert (
        controller.compute(
            measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=_projection()
        ).heat_authority_state
        is PostFcHeatAuthorityState.RECOVERING
    )
    before = controller.snapshot_state()
    released = controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=None)
    assert released.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    controller.restore_state(before)
    assert controller.snapshot_state() == before


@pytest.mark.parametrize(
    "regressing",
    [
        _projection(development_elapsed_seconds=59.0),
        _projection(development_elapsed_seconds=60.0, charge_elapsed_seconds=499.0),
    ],
)
def test_projection_clock_regression_and_expired_entry_runway_cannot_enter(
    regressing: PostFcProjectionInputs,
) -> None:
    """Accepted clocks never move backward, and a passed entry cannot re-enter."""
    controller = PostFcRorController(
        _projection_recovery_config(recovery_trigger_margin_c_per_min=50.0)
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    controller.compute(measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=_projection())
    regressed = controller.compute(
        measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=regressing
    )
    assert regressed.projection_valid is False
    expired = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=95.0),
    )
    assert expired.projection_valid is True
    assert expired.projected_entry_temp_c is None
    assert expired.projected_cutoff_temp_c is not None
    assert expired.recovery_trigger is PostFcRecoveryTrigger.NONE


def test_projection_recovery_remains_active_between_entry_and_cutoff_horizons() -> None:
    """Passing +2 does not release authority before the ratified +5 cutoff."""
    controller = PostFcRorController(
        _projection_recovery_config(
            recovery_confirm_ticks=1,
            recovery_trigger_margin_c_per_min=50.0,
        )
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)

    entered = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=60.0),
    )
    assert entered.recovery_trigger is PostFcRecoveryTrigger.PROJECTION

    between_horizons = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=95.0),
    )
    assert between_horizons.projection_valid is True
    assert between_horizons.projected_entry_temp_c is None
    assert between_horizons.projected_cutoff_temp_c is not None
    assert between_horizons.heat_authority_state is PostFcHeatAuthorityState.RECOVERING
    assert between_horizons.recovery_trigger is PostFcRecoveryTrigger.PROJECTION

    cutoff = controller.compute(
        measured_ror_c_per_min=0.0,
        dt_seconds=5.0,
        projection=_projection(development_elapsed_seconds=110.0),
    )
    assert cutoff.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    assert cutoff.recovery_trigger is PostFcRecoveryTrigger.NONE
    assert controller.snapshot_state().recovery_cutoff_reached is True
    after_cutoff = [
        controller.compute(
            measured_ror_c_per_min=0.0,
            dt_seconds=5.0,
            projection=_projection(development_elapsed_seconds=development),
        )
        for development in (115.0, 120.0)
    ]
    assert [output.effective_ceiling_percent for output in after_cutoff] == [65, 60]
    assert after_cutoff[-1].heat_authority_state is PostFcHeatAuthorityState.HOLDING


def test_v2_fast_raise_preserves_a_ki_zero_bias_handoff() -> None:
    """The no-integral branch carries the v2 one-time floor through bias."""
    controller = PostFcRorController(
        _projection_recovery_config(recovery_confirm_ticks=1, ki_percent_per_ror_second=0.0)
    )
    controller.reset(initial_heat_percent=60, ror_at_engagement_c_per_min=6.0)
    output = controller.compute(
        measured_ror_c_per_min=0.0, dt_seconds=5.0, projection=_projection()
    )
    assert output.heat_percent == 70
    assert output.recovery_fast_raise_applied is True
