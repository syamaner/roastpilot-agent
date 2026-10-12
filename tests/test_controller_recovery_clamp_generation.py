"""Generation-fence coverage for the D96 failed-drop recovery clamp."""

import asyncio
from typing import cast

import pytest

from roastpilot_agent.advisor import FakeAdvisor
from roastpilot_agent.controller import RoastPhase
from roastpilot_agent.models import RoastEventKind
from roastpilot_agent.post_fc_control import PostFcHeatAuthorityState
from roastpilot_agent.safety import SafetyEvaluation
from tests.test_controller import (
    _ISOLATED_CEILING_GUARD_LIMITS,  # pyright: ignore[reportPrivateUsage]
    _AlwaysFailingDropExecutor,  # pyright: ignore[reportPrivateUsage]
    _charge_through_fc_at_heat,  # pyright: ignore[reportPrivateUsage]
    _confirm_recovery_raise,  # pyright: ignore[reportPrivateUsage]
    _recovery_config,  # pyright: ignore[reportPrivateUsage]
    decision,
    make_harness,
    reading,
)


@pytest.mark.asyncio
async def test_estop_during_recovery_clamp_audit_cannot_write_or_exit_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An e-stop after the clamp audit revokes its stale target write.

    A real recovery raise first reaches the fake roaster.  A later advisor
    drop fails, which reaches the recovery clamp with a non-HOLDING output.
    Parking the clamp's own persisted command evaluation makes the generation
    fence the only reason the corrective write and forced recovery exit do
    not happen after the synchronously admitted e-stop.
    """
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=False), decision(heat=50, fan=40, drop=False)],
        default_decision=decision(heat=50, fan=40, drop=True),
    )
    harness = make_harness(
        config=_recovery_config(
            pre_fc_heat_target_percent=60,
            control_interval_seconds=5.0,
            ceiling_guard_temp_c=220.0,
            recovery_trigger_margin_c_per_min=1.0,
            recovery_confirm_ticks=1,
            recovery_headroom_percentage_points=15,
        ),
        advisor=advisor,
        executor=_AlwaysFailingDropExecutor(),
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    assert await _confirm_recovery_raise(harness, entry_heat=60) > 60
    output = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]
    assert output is not None
    assert output.heat_authority_state is PostFcHeatAuthorityState.RECOVERING

    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.sink.persist_evaluation
    state_at_guard = None
    targets_at_guard: list[tuple[int, int]] | None = None
    heat_writes_at_guard: list[int] | None = None
    fan_writes_at_guard: list[int] | None = None
    execute_attempts = 0
    execute_attempts_at_guard: int | None = None
    original_execute_targets = harness.controller._execute_targets  # pyright: ignore[reportPrivateUsage]

    async def observe_execute_targets(
        value: SafetyEvaluation, *, generation: int | None = None
    ) -> bool:
        """Record attempts while delegating unchanged to the real executor path."""
        nonlocal execute_attempts
        execute_attempts += 1
        return await original_execute_targets(value, generation=generation)

    monkeypatch.setattr(harness.controller, "_execute_targets", observe_execute_targets)

    async def park_clamp_evaluation(value: SafetyEvaluation) -> int | None:
        nonlocal execute_attempts_at_guard, fan_writes_at_guard, heat_writes_at_guard
        nonlocal state_at_guard, targets_at_guard
        failed_drop_recorded = any(
            kind is RoastEventKind.COMMAND_FAILED
            and isinstance(payload, dict)
            and cast("dict[str, object]", payload).get("command") == "drop_beans"
            and cast("dict[str, object]", payload).get("source") == "advisor"
            for kind, payload in harness.events.events
        )
        if (
            failed_drop_recorded
            and value.adjusted_heat == 60
            and value.adjusted_fan == harness.controller.snapshot().current_fan
        ):
            output_at_guard = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]
            assert output_at_guard is not None
            assert output_at_guard.heat_authority_state is not PostFcHeatAuthorityState.HOLDING
            state_at_guard = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
            targets_at_guard = list(harness.executor.targets)
            heat_writes_at_guard = list(harness.executor.heat_writes)
            fan_writes_at_guard = list(harness.executor.fan_writes)
            execute_attempts_at_guard = execute_attempts
            entered.set()
            await release.wait()
        return await original(value)

    monkeypatch.setattr(harness.sink, "persist_evaluation", park_clamp_evaluation)
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
    tick = asyncio.create_task(harness.controller.tick())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    prior_generation = harness.controller.command_generation
    assert harness.controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        harness.controller.operator_emergency_stop("recovery clamp audit", already_admitted=True)
    )
    await asyncio.wait_for(stop, timeout=1.0)
    release.set()
    await asyncio.wait_for(tick, timeout=1.0)

    assert harness.controller.phase is RoastPhase.FAULTED
    assert state_at_guard is not None
    assert targets_at_guard is not None
    assert heat_writes_at_guard is not None
    assert fan_writes_at_guard is not None
    assert execute_attempts_at_guard is not None
    assert harness.executor.targets == targets_at_guard
    assert harness.executor.heat_writes == heat_writes_at_guard
    assert harness.executor.fan_writes == fan_writes_at_guard
    assert execute_attempts == execute_attempts_at_guard
    assert harness.controller._post_fc_controller.snapshot_state() == state_at_guard  # pyright: ignore[reportPrivateUsage]
