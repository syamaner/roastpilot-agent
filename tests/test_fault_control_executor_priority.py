"""Exact-session priority entry contract and frozen queued-executor parity."""

from typing import Literal

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import CommandArbiter, FaultControlExecutor
from roastpilot_agent.models import FaultControlState, OperatorAction, RoastCommand, RoastPhase
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict
from tests.test_fault_control_executor_edges import FaultPort, RecordingFaultTrace


class _OrderedFaultPort(FaultPort):
    """Record the ordered port interactions alongside the inherited failure knobs."""

    order: list[str]

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        self.order.append(f"read:{session_id}")
        return await super().read_fault_control_state(session_id)

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        self.order.append(f"write:{expected_session_id}")
        await super().write_fault_control(action, expected_session_id=expected_session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_order", "expected_status", "expected_dispatch", "expected_confirmation"),
    [
        ("success", ["write", "read"], "ok", "completed", "read_back"),
        ("reject", [], "failed", "not_attempted", "failed"),
        ("stale", [], "failed", "not_dispatched", "not_attempted"),
        ("write_failed", ["write"], "failed", "write_failed", "not_attempted"),
        ("read_failed", ["write", "read"], "failed", "completed", "readback_failed"),
        ("read_hot", ["write", "read"], "failed", "completed", "rejected_readback"),
    ],
)
async def test_priority_entry_matches_frozen_queued_exact_emergency_contract(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_order: list[str],
    expected_status: Literal["ok", "failed"],
    expected_dispatch: str,
    expected_confirmation: str,
) -> None:
    """TH1/TH3 pin one admission, exact write/read order, outcomes and audit."""
    session_id = "fault-session"
    state = FaultControlState(
        session_id=session_id,
        mcp_phase="fault",
        active=False,
        device_connected=True,
        heat_level_percent=int(scenario == "read_hot"),
        fan_level_percent=0,
        cooling_on=True,
        beans_added=True,
        beans_dropped=False,
    )
    expected_evaluation = SafetyEvaluation(
        rule="emergency_stop",
        verdict=SafetyVerdict.EMERGENCY_STOP,
        reason="emergency stop requested in phase faulted: terminal fault control",
    )
    if scenario == "reject":
        expected_evaluation = expected_evaluation.model_copy(
            update={"verdict": SafetyVerdict.REJECT}
        )
    expected_trace = (
        RoastCommand.EMERGENCY_STOP,
        expected_status,
        {
            "admission": "rejected" if scenario == "reject" else "allowed",
            "dispatch": expected_dispatch,
            "confirmation": expected_confirmation,
        },
    )
    errors = {
        "stale": "emergency-stop admission is no longer current",
        "write_failed": "synthetic fault-control write failure",
        "read_failed": "synthetic exact readback failure",
    }

    async def exercise(priority: bool) -> tuple[object, ...]:
        safety = SafetyPolicy(SafetyLimits())
        arbiter = CommandArbiter()
        evaluation_order: list[tuple[RoastPhase, str | None]] = []
        original_admit = arbiter.admit_emergency_stop
        original_evaluate = safety.evaluate_emergency_stop
        admissions = 0

        def admit() -> int:
            nonlocal admissions
            admissions += 1
            return original_admit()

        def evaluate(*, phase: RoastPhase, operator_reason: str | None = None) -> SafetyEvaluation:
            evaluation_order.append((phase, operator_reason))
            measured = original_evaluate(phase=phase, operator_reason=operator_reason)
            if scenario == "reject":
                return measured.model_copy(update={"verdict": SafetyVerdict.REJECT})
            return measured

        monkeypatch.setattr(arbiter, "admit_emergency_stop", admit)
        monkeypatch.setattr(safety, "evaluate_emergency_stop", evaluate)
        trace = RecordingFaultTrace()
        port = _OrderedFaultPort(
            state, fail_write=scenario == "write_failed", fail_readback=scenario == "read_failed"
        )
        port.order = []
        executor = FaultControlExecutor(safety, port, arbiter=arbiter, trace_sink=trace)
        generation = arbiter.admit_emergency_stop()

        def current() -> bool:
            return scenario != "stale"

        async def execute() -> FaultControlState | None:
            if priority:
                return await executor.execute_priority_emergency_stop(
                    session_id, run_id="fault-run", is_current_generation=current
                )
            return await executor.execute(
                OperatorAction.EMERGENCY_STOP,
                session_id,
                run_id="fault-run",
                is_current_generation=current,
                emergency_generation=generation,
            )

        result: FaultControlState | str | None
        try:
            result = await execute()
        except RuntimeError as error:
            result = str(error)
        finally:
            assert await executor.settle_for_shutdown()

        assert admissions == 1 and not arbiter.emergency_stop_active
        if priority:
            assert executor._worker is None and executor._sequence == 0  # pyright: ignore[reportPrivateUsage]
        assert evaluation_order == [(RoastPhase.FAULTED, "terminal fault control")]
        assert result == (state if scenario == "success" else errors.get(scenario))
        assert port.order == [f"{action}:{session_id}" for action in expected_order]
        assert trace.evaluations == [expected_evaluation]
        assert trace.commands == [expected_trace]
        return result, port.order, trace.evaluations, trace.commands

    assert await exercise(False) == await exercise(True)
