"""Tests for the closed-loop two-node plant harness (#1131, slice 3a)."""

from __future__ import annotations

import pytest

from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.fc_prediction import (
    HeaterLagFilter,
    TwoNodeParameters,
    two_node_fc_eta_seconds,
)
from roastpilot_agent.models import RoastPhase, RoastProfile
from tests.sim_two_node_plant import (
    PERTURBED_PLANTS,
    WINTER_FC_BEAN_C,
    WINTER_START,
    PlantRoaster,
    TwoNodePlant,
    run_closed_loop,
)

PROFILE = RoastProfile(
    name="winter-sim",
    bean_origin="Brazil",
    bean_weight_grams=250.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    target_drop_temp_c=195.0,
    target_development_percent=14.0,
)


def test_plant_matches_the_model_with_the_heater_lag_in_play() -> None:
    """QA: start off-equilibrium (Q=0.6, hold 70 %) so the lag and tau matter;
    the plant crosses 176 °C when the module's model says it will."""
    params = TwoNodeParameters()
    bean, env, heater = 150.0, 215.0, 0.6
    eta = two_node_fc_eta_seconds(
        bean_temp_c=bean,
        env_temp_c=env,
        heater_state=heater,
        hold_heat_percent=70.0,
        ambient_temp_c=19.5,
        fc_target_bean_temp_c=176.0,
        parameters=params,
    )
    assert eta is not None
    plant = TwoNodePlant(params, ambient_c=19.5, bean_c=bean, env_c=env, heater_state=heater)
    plant.heat_fraction = 0.7
    seconds = 0
    while plant.bean_c < 176.0:
        plant.step()
        seconds += 1
    assert seconds == pytest.approx(eta, abs=1.0)


def test_plant_heater_state_follows_the_module_filter() -> None:
    """The plant's Q evolves exactly as HeaterLagFilter replays applied heat."""
    params = TwoNodeParameters(tau_seconds=50.0)
    plant = TwoNodePlant(params, ambient_c=20.0, bean_c=150.0, env_c=200.0, heater_state=1.0)
    lag = HeaterLagFilter(params)
    lag.update(elapsed_seconds=0.0, heat_percent=100.0)
    lag.update(elapsed_seconds=0.0, heat_percent=30.0)
    plant.heat_fraction = 0.3
    for _ in range(40):
        plant.step()
    assert plant.heater_state == pytest.approx(lag.update(elapsed_seconds=40.0, heat_percent=30.0))


def test_heat_step_reaches_the_bean_only_through_the_lag() -> None:
    """QA: a heat cut moves Q first, the environment next and the bean last."""
    params = TwoNodeParameters()
    start = {"ambient_c": 20.0, "bean_c": 160.0, "env_c": 215.0, "heater_state": 1.0}
    cut, hold = TwoNodePlant(params, **start), TwoNodePlant(params, **start)
    cut.heat_fraction = 0.0
    hold.heat_fraction = 1.0
    cut.step()
    hold.step()
    assert (cut.bean_c, cut.env_c) == (hold.bean_c, hold.env_c)  # derivatives used old Q
    assert cut.heater_state < hold.heater_state
    cut.step()
    hold.step()
    assert cut.env_c < hold.env_c and cut.bean_c == hold.bean_c
    cut.step()
    hold.step()
    assert cut.bean_c < hold.bean_c


def test_first_reported_ror_is_the_plant_slope_not_an_artefact() -> None:
    import asyncio

    params = TwoNodeParameters()
    plant = TwoNodePlant(params, ambient_c=19.5, bean_c=150.0, env_c=210.0, heater_state=1.0)
    telemetry = asyncio.run(PlantRoaster(plant).read_telemetry())
    assert telemetry is not None and telemetry.bean_ror_c_per_min is not None
    slope_per_min = (params.kb * (210.0 - 150.0) - params.cb) * 60.0
    assert telemetry.bean_ror_c_per_min == pytest.approx(slope_per_min, rel=1e-6)


def test_dropped_plant_stops_and_heat_writes_drive_the_plant() -> None:
    plant = TwoNodePlant(
        TwoNodeParameters(), ambient_c=20.0, bean_c=150.0, env_c=200.0, heater_state=1.0
    )
    roaster = PlantRoaster(plant)
    import asyncio

    asyncio.run(roaster.set_targets(heat_percent=40, fan_percent=30))
    assert plant.heat_fraction == pytest.approx(0.4)
    asyncio.run(roaster.set_fan(50))  # fan-only, no pending heat: heat must hold
    assert plant.heat_fraction == pytest.approx(0.4)
    assert roaster.targets[-1] == (40, 50)
    asyncio.run(roaster.set_heat(70))
    assert plant.heat_fraction == pytest.approx(0.7)
    asyncio.run(roaster.set_fan(60))  # a fan-only write keeps the heat
    assert plant.heat_fraction == pytest.approx(0.7)
    asyncio.run(roaster.emergency_stop(reason="test"))
    assert plant.heat_fraction == 0.0
    asyncio.run(roaster.set_heat(50))
    asyncio.run(roaster.contain_heat_fixed_zero())
    assert plant.heat_fraction == 0.0
    asyncio.run(roaster.drop_beans())
    frozen = (plant.bean_c, plant.env_c)
    roaster.advance()
    assert (plant.bean_c, plant.env_c) == frozen


@pytest.mark.asyncio
async def test_incumbent_controller_closes_the_loop_through_first_crack_to_a_drop() -> None:
    """Today's controller (finish control off) on the winter plant at 19.5 °C:
    it reaches FC from the synthetic detection, develops, and drops."""
    result = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=TwoNodeParameters(),
        ambient_c=19.5,
    )
    assert result.first_crack_charge_s is not None
    assert result.drop_charge_s is not None and result.drop_bean_c is not None
    assert result.drop_bean_c >= 195.0
    phases = [t[5] for t in result.ticks]
    assert RoastPhase.DEVELOPMENT in phases
    assert len({t[3] for t in result.ticks}) > 1  # the controller moved heat
    assert result.bean_at(result.ticks[0][0]) == pytest.approx(WINTER_START[1], abs=0.5)
    dev = result.development_percent_at_bean(195.0)
    assert dev is not None and 0.0 < dev < 100.0


@pytest.mark.asyncio
async def test_closed_loop_is_deterministic_and_sensitive_to_the_plant() -> None:
    async def run(params: TwoNodeParameters, room: float) -> tuple[object, ...]:
        r = await run_closed_loop(
            config=ControllerConfig(), profile=PROFILE, plant_parameters=params, ambient_c=room
        )
        return (r.first_crack_charge_s, r.drop_charge_s, r.drop_bean_c, len(r.ticks))

    base = await run(TwoNodeParameters(), 19.5)
    assert base == await run(TwoNodeParameters(), 19.5)
    assert base != await run(PERTURBED_PLANTS["slow_heater"], 19.5)
    assert base != await run(TwoNodeParameters(), 25.0)


def test_scoring_helpers_handle_missing_endpoints() -> None:
    from tests.sim_two_node_plant import ClosedLoopResult

    empty = ClosedLoopResult()
    assert empty.bean_at(100.0) is None
    assert empty.development_percent_at_bean(195.0) is None
    partial = ClosedLoopResult(
        ticks=[(200.0, 150.0, 200.0, 100, 30, RoastPhase.ROASTING_PRE_FIRST_CRACK)]
    )
    assert partial.development_percent_at_bean(195.0) is None  # no FC, never reached


@pytest.mark.asyncio
async def test_incumbent_reproduces_the_7_nov_winter_miss_with_a_late_mark() -> None:
    """Regression anchor for slice 3: with first crack at the winter mark's
    ~189 °C, today's controller ends on the 196 °C guard well short of the
    14 % development target, as the 7 Nov roast did (7.4 %)."""
    result = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=TwoNodeParameters(),
        ambient_c=19.5,
        first_crack_bean_c=WINTER_FC_BEAN_C,
    )
    assert result.drop_bean_c is not None and result.drop_bean_c >= 196.0
    assert (result.end_reason, result.drop_reason) == ("drop", "ceiling_guard")
    dev = result.development_percent_at_bean(195.0)
    assert dev is not None and 0.0 < dev < 12.0


@pytest.mark.asyncio
async def test_perturbed_plants_end_truthfully() -> None:
    """A strong heater trips the 240 °C environment limit (a fault, heat off);
    a weak heater runs out of time short of the target. Both are recorded."""
    strong = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=PERTURBED_PLANTS["strong_heater"],
        ambient_c=19.5,
    )
    assert (strong.end_phase, strong.end_reason) == (RoastPhase.FAULTED, "fault")
    assert strong.emergency_stop_reasons  # the fault's own heat-off stop
    assert strong.ticks[-1][3] == 0
    weak = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=PERTURBED_PLANTS["weak_heater"],
        ambient_c=19.5,
        max_seconds=300,
    )
    assert weak.drop_charge_s is None
    assert (weak.end_phase, weak.end_reason) == (RoastPhase.ROASTING_PRE_FIRST_CRACK, "timeout")


@pytest.mark.asyncio
async def test_detection_lag_delays_the_controller_first_crack() -> None:
    """QA: with a 15 s detector lag the controller enters development 15 s
    after the crack, as on the real roaster."""
    prompt = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=TwoNodeParameters(),
        ambient_c=19.5,
    )
    lagged = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=TwoNodeParameters(),
        ambient_c=19.5,
        fc_detection_lag_s=15,
    )
    assert prompt.first_crack_charge_s is not None and lagged.first_crack_charge_s is not None
    assert lagged.first_crack_charge_s - prompt.first_crack_charge_s == pytest.approx(15.0, abs=1.0)
