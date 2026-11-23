"""Tests for the closed-loop two-node plant harness (#1131, slice 3a)."""

from __future__ import annotations

import pytest

from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.fc_prediction import TwoNodeParameters, two_node_fc_eta_seconds
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


def test_plant_matches_the_model_used_by_the_shadow_eta() -> None:
    """The plant integrates exactly the model ``two_node_fc_eta_seconds``
    projects: stepping it at held heat crosses 176 °C when the ETA said."""
    params = TwoNodeParameters()
    _, bean, env, heater = WINTER_START
    eta = two_node_fc_eta_seconds(
        bean_temp_c=bean,
        env_temp_c=env,
        heater_state=heater,
        hold_heat_percent=100.0,
        ambient_temp_c=19.5,
        fc_target_bean_temp_c=176.0,
        parameters=params,
    )
    assert eta is not None
    plant = TwoNodePlant(params, ambient_c=19.5, bean_c=bean, env_c=env, heater_state=heater)
    seconds = 0
    while plant.bean_c < 176.0:
        plant.step()
        seconds += 1
    assert seconds == pytest.approx(eta, abs=1.0)


def test_dropped_plant_stops_and_heat_writes_drive_the_plant() -> None:
    plant = TwoNodePlant(
        TwoNodeParameters(), ambient_c=20.0, bean_c=150.0, env_c=200.0, heater_state=1.0
    )
    roaster = PlantRoaster(plant)
    import asyncio

    asyncio.run(roaster.set_targets(heat_percent=40, fan_percent=30))
    assert plant.heat_fraction == pytest.approx(0.4)
    asyncio.run(roaster.set_heat(70))
    assert plant.heat_fraction == pytest.approx(0.7)
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
    dev = result.development_percent_at_bean(195.0)
    assert dev is not None and dev < 12.0


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
    assert strong.end_phase is RoastPhase.FAULTED
    assert strong.ticks[-1][3] == 0
    weak = await run_closed_loop(
        config=ControllerConfig(),
        profile=PROFILE,
        plant_parameters=PERTURBED_PLANTS["weak_heater"],
        ambient_c=19.5,
        max_seconds=300,
    )
    assert weak.drop_charge_s is None
    assert weak.end_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
