"""E9-S2 vertical slice — the milestone flow against the **real**
``coffee-roaster-mcp`` server spawned as a stdio subprocess in mock-driver mode
(component plan §8; orchestration plan § First Milestone; D6).

Same closed loop as E9-S1 (`test_milestone1.py`) but the controller drives the
real MCP child — no fake reader/executor. Hardware-free: the mock roaster driver
with ``first_crack_mode=disabled`` is bootstrap-safe (no Hottop, no microphone,
no model download). Skipped automatically when the server binary is not
installed (CI installs ``coffee-roaster-mcp`` for this slice).

The mock driver simulates its own temperature curve (it does not inject scripted
frames), so the run is driven the way a real roast is: a context-aware advisor
ramps heat then cuts it to engineer the charge-temperature drop the server's
auto-T0 detector needs, first crack is the operator override (audio detection is
disabled), and drop/stop-cooling are operator actions. Deterministic: the mock
advances exactly one virtual second per state read.
"""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.api import RoastService
from roastpilot_agent.config import AppConfig, ControllerConfig, MCPConfig
from roastpilot_agent.mcp_client import MCPServerProcess, RoasterControlAdapter, RoasterMCPClient
from roastpilot_agent.models import (
    AdvisorHealth,
    AdvisorHealthStatus,
    OperatorAction,
    OperatorActionRequest,
    RoastEventKind,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.store import RoastStore
from tests.conftest import FakeClock

pytestmark = [
    pytest.mark.serial(reason="drives a real MCP child and verifies process teardown"),
    pytest.mark.skipif(
        shutil.which("coffee-roaster-mcp") is None,
        reason="coffee-roaster-mcp not installed (the E9-S2 real-subprocess slice)",
    ),
]

# A bootstrap-safe mock config that enables auto-T0 (config-file only) with a
# small drop threshold so the engineered charge drop trips it quickly — keeping
# the CI run short and deterministic.
_CONFIG_YAML = """
roaster:
  driver: mock
first_crack:
  mode: disabled
session:
  auto_t0_detection_enabled: true
  auto_t0_drop_threshold_c: 5
"""

_CHARGE_RAMP_CEILING_C = 55.0


class _ChargeDropAdvisor(RoastAdvisor):
    """Drives the mock roaster the way a charge does: ramp heat until the beans
    warm past the charge ceiling, then cut heat and lift fan so the simulated
    bean temperature falls — the drop the server's auto-T0 detector needs. Stays
    well below the pre-T0 overrun bound (200 °C); the controller's safety policy
    still validates and clamps every target."""

    @property
    def descriptor(self) -> AdvisorDescriptor:
        return AdvisorDescriptor(provider="test", model="charge-drop", prompt_version="t")

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        if context.current_bean_temp_c < _CHARGE_RAMP_CEILING_C:
            return RoastDecision(
                target_heat=100,
                target_fan=0,
                should_drop=False,
                confidence=0.9,
                rationale="ramp toward the charge ceiling",
            )
        return RoastDecision(
            target_heat=0,
            target_fan=100,
            should_drop=False,
            confidence=0.9,
            rationale="cut heat to drop bean temp and trip auto-T0",
        )

    async def healthcheck(self) -> AdvisorHealth:
        return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE)


def _profile() -> RoastProfile:
    return RoastProfile(
        name="Mock Slice",
        bean_origin="Ethiopia",
        bean_varietal="Heirloom",
        bean_weight_grams=250.0,
        charge_guidance_min_c=40.0,
        charge_guidance_max_c=60.0,
        initial_heat_percent=100,  # start the ramp immediately
        initial_fan_percent=0,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    instance = RoastStore(tmp_path / "milestone-real.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_vertical_slice_against_real_mcp_subprocess(
    store: RoastStore, tmp_path: Path
) -> None:
    """The milestone flow end to end against the real coffee-roaster-mcp child.

    The MCP child is started and stopped within this one coroutine: the stdio
    transport's anyio cancel scope must be entered and exited in the same task,
    so it cannot live in a setup/teardown fixture."""
    config_path = tmp_path / "coffee-roaster-mcp.yaml"
    config_path.write_text(_CONFIG_YAML, encoding="utf-8")
    real_mcp = MCPServerProcess(MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(config_path)}))
    await real_mcp.start()
    try:
        await _drive_slice(real_mcp, store)
    finally:
        await real_mcp.stop()


async def _drive_slice(real_mcp: MCPServerProcess, store: RoastStore) -> None:
    # The server must be the bootstrap-safe mock (no hardware/audio/model).
    info = await RoasterMCPClient(real_mcp.call_tool).get_server_info()
    assert info.bootstrap_safe is True
    assert info.roaster_driver == "mock"

    adapter = RoasterControlAdapter(RoasterMCPClient(real_mcp.call_tool))
    clock = FakeClock()
    config = AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0))
    service = RoastService(
        store,
        config=config,
        mcp=real_mcp,
        roaster=adapter,
        advisor=_ChargeDropAdvisor(),
        exporter=adapter,
        raw_state=adapter,
        run_loop=False,
        clock=clock,
    )

    # Start the roast against the live child → preheating, MCP child running.
    detail = await service.start_roast(_profile())
    run_id = detail.id
    assert detail.agent_phase is RoastPhase.PREHEATING
    assert service.mcp_child_status().value == "running"

    async def tick() -> bool:
        assert service.runner is not None
        clock.advance(3.0)  # clear the 2 s command rate limit
        return await service.runner.tick_once()

    async def run_until(phase: RoastPhase, *, cap: int) -> None:
        for _ in range(cap):
            await tick()
            if (await service.detail(run_id)).agent_phase is phase:
                return
        actual = (await service.detail(run_id)).agent_phase
        raise AssertionError(
            f"did not reach {phase.value} within {cap} ticks (stuck at {actual.value})"
        )

    # D32 (#191): the advisor is OFF in preheat, so reaching T0 is the OPERATOR's
    # charge action (mark_beans_added — the manual-T0 fallback), not an
    # advisor-driven heat cut. Charge promptly, before the profile's 100 % warm-up
    # heat overruns the pre-T0 ceiling, then advance to roasting_pre_first_crack.
    charge = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.MARK_BEANS_ADDED)
    )
    assert charge.result == "accepted"
    await run_until(RoastPhase.ROASTING_PRE_FIRST_CRACK, cap=10)

    # First crack is the operator override (audio detection disabled).
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.MARK_FIRST_CRACK)
    )
    assert result.result == "accepted"
    await run_until(RoastPhase.DEVELOPMENT, cap=5)

    # Drop, then stop cooling — operator actions through full safety on drain.
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    await run_until(RoastPhase.COOLING, cap=5)
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    finalized = await tick()
    assert finalized

    completed = await service.detail(run_id)
    assert completed.agent_phase is RoastPhase.COMPLETE
    assert completed.outcome == "completed"
    assert completed.export_manifest is not None  # the real server exported logs

    # The decision trace is persisted and readable via the timeline route.
    timeline = await service.timeline(run_id)
    kinds = {e.kind for e in timeline.events}
    assert {
        RoastEventKind.RUN_STARTED,
        RoastEventKind.T0_DETECTED,
        RoastEventKind.FIRST_CRACK,
        RoastEventKind.RUN_COMPLETED,
        RoastEventKind.LOGS_EXPORTED,
    } <= kinds
    assert timeline.safety_evaluations
    assert timeline.commands  # operator commands in command_log

    await service.shutdown()
    # A restart re-reads the completed run; it is recoverable and not active.
    assert (await store.active_run()) is None
