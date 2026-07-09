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

import pytest

from roastpilot_agent.config import PostFirstCrackControl
from roastpilot_agent.post_fc_control import (
    PostFcControllerState,
    PostFcControlOutput,
    PostFcRorController,
)


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
    alone)."""
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

    # Simulate the measured RoR gently decaying alongside the taper (a
    # roast responding as expected to the held-then-eased heat), roughly
    # tracking the setpoint each tick.
    heats = [tick1.heat_percent]
    ror = 6.1
    for _ in range(16):  # 16 * 5s = 80s, most of the 90s taper
        output = controller.compute(measured_ror_c_per_min=ror, dt_seconds=5.0)
        heats.append(output.heat_percent)
        ror = max(4.0, ror - 0.15)  # RoR eases down, tracking the taper

    # The 91% runaway is structurally impossible: the ceiling is 72 for the
    # entire engagement.
    assert all(h <= 72 for h in heats), heats
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
    Deleting either one leaves half the clamp unproven."""
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
    for _ in range(10):
        output = controller.compute(measured_ror_c_per_min=12.0, dt_seconds=5.0)
        phase1_heats.append(output.heat_percent)
    assert phase1_heats[-1] < 72  # genuinely eased below the ceiling

    # Phase 2: RoR collapses (stall) -> the loop recovers (raises heat back
    # up) but never past the never-add-heat-beyond-entry ceiling (72).
    phase2_heats: list[int] = []
    for _ in range(20):
        output = controller.compute(measured_ror_c_per_min=2.0, dt_seconds=5.0)  # stalled RoR
        phase2_heats.append(output.heat_percent)

    assert max(phase2_heats) <= 72
    assert phase2_heats[-1] > phase1_heats[-1]  # recovering, not frozen


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
    low_seed = PostFcControllerState(
        integrator=50.0,  # ki*50 = 5, well below the 30 floor at zero error
        bias_percent=low_seed.bias_percent,
        ema=low_seed.ema,
        taper_elapsed_seconds=low_seed.taper_elapsed_seconds,
        taper_r0_c_per_min=low_seed.taper_r0_c_per_min,
        heat_engage_percent=low_seed.heat_engage_percent,
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
