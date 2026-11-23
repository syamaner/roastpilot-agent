"""Closed-loop two-node roaster plant for controller simulations (D239 slice 3a, #1131).

A deterministic stand-in for the Hottop: the fitted two-node bean/environment
model (``roastpilot_agent.fc_prediction.TwoNodeParameters``) driven at 1 s by
the heat the controller actually commands, with the heater lag. It implements
the controller's ``StateReader`` and ``CommandExecutor`` seams, so the real
``RoastController.tick()`` loop runs against it on a ``FakeClock``.

What it can show: pre-FC approach, thermal-lag handling and finish timing for
a control law. What it cannot: post-FC exotherm (not modelled), fan effect (fan
was 30 % on every fitted roast, so ``g``/``ka`` absorb it), or real-world
accuracy (the controller may use the same model it is tested against; use a
perturbed plant, ``PERTURBED_PLANTS``, to test robustness). Celsius
throughout. Test-only: it never reaches a real roaster.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import RoastController
from roastpilot_agent.fc_prediction import TwoNodeParameters
from roastpilot_agent.models import (
    AppliedRoasterState,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyPolicy
from tests.conftest import EventSink, FakeClock, RecordingExecutor, RecordingSnapshotSink

_FITTED = TwoNodeParameters()

#: Plants that deliberately differ from the controller's default model. The
#: leave-one-out fold extremes trade their constants off against each other and
#: barely change the closed loop, so robustness is tested with direct heater
#: perturbations instead: a weaker or stronger heater, and a slower heater lag.
PERTURBED_PLANTS: dict[str, TwoNodeParameters] = {
    "weak_heater": replace(_FITTED, g=_FITTED.g * 0.75),
    "strong_heater": replace(_FITTED, g=_FITTED.g * 1.25),
    "slow_heater": replace(_FITTED, tau_seconds=_FITTED.tau_seconds * 1.5),
}

#: The bean temperature of the operator's 7 Nov first-crack mark (about
#: 189 °C, possibly late). With it, today's controller reproduces the winter
#: miss on this plant: the 196 °C guard drops at about 9 % development.
WINTER_FC_BEAN_C = 189.0

#: The 7 Nov 2026 winter roast at BT 120 °C (charge +196 s, ET 190 °C, 100 %
#: heat): the earliest point inside the model's fitted range (BT >= 120 °C).
WINTER_START = (196.0, 120.0, 190.0, 1.0)


class TwoNodePlant:
    """The plant state, integrated in 1 s steps exactly as the study's model."""

    def __init__(
        self,
        parameters: TwoNodeParameters,
        *,
        ambient_c: float,
        bean_c: float,
        env_c: float,
        heater_state: float,
    ) -> None:
        """Start the plant at a charge-referenced state.

        Args:
            parameters: The plant's constants.
            ambient_c: The room temperature.
            bean_c: Initial bean temperature.
            env_c: Initial environment temperature.
            heater_state: Initial heater state Q (0-1).
        """
        self.parameters = parameters
        self.ambient_c = ambient_c
        self.bean_c = bean_c
        self.env_c = env_c
        self.heater_state = heater_state
        self.heat_fraction = heater_state
        self.dropped = False

    def step(self) -> None:
        """Advance one second; a dropped plant stops changing."""
        if self.dropped:
            return
        p = self.parameters
        db = p.kb * (self.env_c - self.bean_c) - p.cb
        de = (
            p.g * self.heater_state
            - p.ka * (self.env_c - self.ambient_c)
            - p.ke * (self.env_c - self.bean_c)
        )
        self.heater_state += (self.heat_fraction - self.heater_state) / p.tau_seconds
        self.bean_c += db
        self.env_c += de


class PlantRoaster(RecordingExecutor):
    """``StateReader`` + ``CommandExecutor`` backed by a ``TwoNodePlant``.

    Commanded heat drives the plant; telemetry reports its state, a 30 s
    bean RoR, the room reading, and a synthetic MCP first-crack detection once
    the bean reaches ``first_crack_bean_c``.
    """

    def __init__(self, plant: TwoNodePlant, *, first_crack_bean_c: float = 182.0) -> None:
        """Wrap a plant.

        Args:
            plant: The plant to drive.
            first_crack_bean_c: Bean temperature at which MCP reports FC.
        """
        super().__init__()
        self.plant = plant
        self.first_crack_bean_c = first_crack_bean_c
        self._bean_history: list[float] = [plant.bean_c]

    def advance(self) -> None:
        """One plant second; record the bean for the RoR window."""
        self.plant.step()
        self._bean_history.append(self.plant.bean_c)

    async def read_telemetry(self) -> RoastTelemetry | None:
        """Report the plant's current state as MCP telemetry."""
        window = self._bean_history[-31:]
        ror = (window[-1] - window[0]) * 60.0 / max(len(window) - 1, 1)
        return RoastTelemetry.model_validate(
            {
                "bean_temp_c": self.plant.bean_c,
                "env_temp_c": self.plant.env_c,
                "bean_ror_c_per_min": ror,
                "ambient_temp_c": self.plant.ambient_c,
                "ambient_age_seconds": 1.0,
                "first_crack_detected": self.plant.bean_c >= self.first_crack_bean_c,
                "cooling_on": self.plant.dropped,
            }
        )

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        """Apply commanded heat to the plant, then record it."""
        self.plant.heat_fraction = heat_percent / 100.0
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)

    async def set_heat(self, heat_percent: int) -> None:
        """Apply commanded heat to the plant, then record it."""
        self.plant.heat_fraction = heat_percent / 100.0
        await super().set_heat(heat_percent)

    async def drop_beans(self) -> AppliedRoasterState | None:
        """Drop: the plant stops evolving; heat off."""
        self.plant.dropped = True
        self.plant.heat_fraction = 0.0
        return await super().drop_beans()


#: One tick of a closed-loop trace: (charge_s, bean_c, env_c, heat, fan, phase).
Tick = tuple[float, float, float, int, int, RoastPhase]


@dataclass
class ClosedLoopResult:
    """The trace and endpoints of one closed-loop run.

    Attributes:
        ticks: ``(charge_s, bean_c, env_c, heat, fan, phase)`` per tick.
        first_crack_charge_s: Charge time the controller entered DEVELOPMENT.
        drop_charge_s: Charge time of the drop, or ``None``.
        drop_bean_c: Bean temperature at the drop, or ``None``.
        end_phase: The controller phase when the run ended (a drop, a fault,
            or the time limit).
    """

    ticks: list[Tick] = field(default_factory=lambda: list[Tick]())
    first_crack_charge_s: float | None = None
    drop_charge_s: float | None = None
    drop_bean_c: float | None = None
    end_phase: RoastPhase | None = None

    def bean_at(self, charge_s: float) -> float | None:
        """Bean temperature at the last tick at or before ``charge_s``."""
        before = [t for t in self.ticks if t[0] <= charge_s]
        return before[-1][1] if before else None

    def development_percent_at_bean(self, target_c: float) -> float | None:
        """Development % (from the controller's FC) when the bean first reaches
        ``target_c``, or ``None`` if it never did or FC never happened."""
        fc = self.first_crack_charge_s
        reached = next((t for t in self.ticks if t[1] >= target_c), None)
        if fc is None or reached is None or reached[0] <= 0:
            return None
        return 100.0 * (reached[0] - fc) / reached[0]


async def run_closed_loop(
    *,
    config: ControllerConfig,
    profile: RoastProfile,
    plant_parameters: TwoNodeParameters,
    ambient_c: float,
    start: tuple[float, float, float, float] = WINTER_START,
    max_seconds: int = 900,
    first_crack_bean_c: float = 182.0,
) -> ClosedLoopResult:
    """Run the real controller tick loop against the plant from ``start``.

    The controller starts already charged (pre-FC, charge clock set so the
    first tick is at ``start[0]`` seconds after charge) at full heat.

    Args:
        config: The controller configuration under test.
        profile: The roast profile (drop target, development target).
        plant_parameters: The plant's constants (may differ from the model).
        ambient_c: Room temperature.
        start: ``(charge_s, bean_c, env_c, heater_state)``.
        max_seconds: Stop after this many ticks.
        first_crack_bean_c: Bean temperature at which MCP reports FC.

    Returns:
        The closed-loop trace and endpoints.
    """
    charge_s, bean_c, env_c, heater = start
    clock = FakeClock()
    plant = TwoNodePlant(
        plant_parameters, ambient_c=ambient_c, bean_c=bean_c, env_c=env_c, heater_state=heater
    )
    roaster = PlantRoaster(plant, first_crack_bean_c=first_crack_bean_c)
    controller = RoastController(
        config=config,
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=roaster,
        command_executor=roaster,
        snapshot_sink=RecordingSnapshotSink(),
        event_emitter=EventSink(),
        clock=clock,
    )
    controller.load_profile(profile)
    for phase in (RoastPhase.STARTING, RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK):
        controller.transition_to(phase)
    controller._charge_monotonic = clock.now - charge_s  # pyright: ignore[reportPrivateUsage]
    controller._current_heat = round(heater * 100)  # pyright: ignore[reportPrivateUsage]
    result = ClosedLoopResult()
    for _ in range(max_seconds):
        await controller.tick()
        snap = controller.snapshot()
        now_s = clock.now + charge_s
        if result.first_crack_charge_s is None and snap.phase is RoastPhase.DEVELOPMENT:
            result.first_crack_charge_s = now_s
        result.ticks.append(
            (now_s, plant.bean_c, plant.env_c, snap.current_heat, snap.current_fan, snap.phase)
        )
        result.end_phase = snap.phase
        if plant.dropped:
            result.drop_charge_s = now_s
            result.drop_bean_c = plant.bean_c
            break
        if snap.phase not in (RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT):
            break
        roaster.advance()
        clock.advance(1.0)
    return result
