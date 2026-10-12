"""Focused regression tests for independently triaged PR1 safety repairs."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoasterControl, RoastService
from roastpilot_agent.config import AppConfig, SafetyLimits
from roastpilot_agent.controller import FaultControlExecutor
from roastpilot_agent.models import (
    FaultControlsActionRequest,
    FaultControlState,
    OperatorAction,
    OperatorActionRequest,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.safety import SafetyPolicy
from roastpilot_agent.store import RoastStore


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    """Return an initialised temporary decision-trace store."""
    instance = RoastStore(tmp_path / "safety-repair.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def _profile() -> RoastProfile:
    """Return the minimal valid profile for a synthetic safety row."""
    return RoastProfile(
        name="Safety repair",
        bean_origin="Test",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=200.0,
        target_development_percent=14.0,
    )


class _FaultPort:
    """Exact-session port whose state and write outcome are test-controlled."""

    def __init__(self, *, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.writes: list[OperatorAction] = []
        self.dropped = False

    def _state(self) -> FaultControlState:
        return FaultControlState(
            session_id="fault-session",
            mcp_phase="fault",
            active=False,
            device_connected=True,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            beans_added=True,
            beans_dropped=self.dropped,
        )

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return a stable exact fault-session state."""
        assert session_id == "fault-session"
        return self._state()

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the same state as the latest exact session."""
        return self._state()

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Record, or deliberately fail, one guarded write."""
        assert expected_session_id == "fault-session"
        self.writes.append(action)
        if self.fail_write:
            raise RuntimeError("synthetic fault-control write failure")
        if action is OperatorAction.DROP_BEANS:
            self.dropped = True


@pytest.mark.asyncio
async def test_active_nonfaulted_open_lease_rejects_terminal_controls_before_executor(
    store: RoastStore,
) -> None:
    """A synthetic OPEN lease cannot redirect a live non-faulted drop."""
    run_id = "active-nonfaulted"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    port = _FaultPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - durable fixture

    projection = await service._fault_controls_projection(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        lease
    )
    assert projection.enabled_actions == [OperatorAction.EMERGENCY_STOP]
    result = await service.submit_operator_action(
        run_id, request=OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )

    assert result.result == "rejected"
    assert port.writes == []
    assert service.operator_queue.empty()


@pytest.mark.asyncio
async def test_terminal_fault_write_persists_linked_admission_and_failure_trace(
    store: RoastStore,
) -> None:
    """Terminal exact-session failure retains a linked safety and command trace."""
    run_id = "terminal-trace"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = _FaultPort(fail_write=True)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=store)

    with pytest.raises(RuntimeError, match="synthetic fault-control write failure"):
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )

    timeline = await store.read_timeline(run_id)
    assert len(timeline.safety_evaluations) == 1
    assert len(timeline.commands) == 1
    command = timeline.commands[0]
    assert command.status == "failed"
    assert command.safety_evaluation_id == timeline.safety_evaluations[0].id
    assert command.result == {
        "admission": "allowed",
        "confirmation": "unconfirmed",
        "dispatch": "failed",
    }


@pytest.mark.asyncio
async def test_terminal_historical_open_lease_keeps_exact_session_route(store: RoastStore) -> None:
    """A completed fault row still uses its retained exact-session control."""
    run_id = "terminal-historical"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
    port = _FaultPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )

    assert result.result == "confirmed"
    assert port.writes == [OperatorAction.DROP_BEANS]


@pytest.mark.asyncio
async def test_restart_required_detail_projects_only_emergency_stop(store: RoastStore) -> None:
    """A recovered non-faulted row never offers actions the D213 gate rejects."""
    run_id = "restart-required"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    )
    service = RoastService(
        store, roaster=cast(RoasterControl, _FaultPort()), live_serve_mode=True, run_loop=False
    )

    detail = await service.detail(run_id)

    assert detail.enabled_actions == [OperatorAction.EMERGENCY_STOP]
