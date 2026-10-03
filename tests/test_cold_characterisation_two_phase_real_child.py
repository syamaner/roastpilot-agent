"""T-RC: two-phase child ownership against the real ``coffee-roaster-mcp`` 0.2.2 mock.

Software integration evidence only, never recording-on acceptance.  One
``MCPServerProcess`` and one cold client are driven across three asyncio tasks
through the orchestrator's own child state machine: recording-off spawn, cold
session, activation, one read and D195 finalisation; a confirmed stop, the
recording-on respawn and the same cycle on the same client; then the final
confirmed stop.  The mock driver, disabled first-crack audio and disabled
ambient sensing are explicit in both the rendered yaml and the environment, so no
hardware, microphone, model download or sensor is touched.  Recording-on flags
are rendered but inert here (first crack is disabled).  Phase admission is not
attempted: the frozen identity refuses a disabled first-crack mode, which is
the honest scope of this test.

Published 0.2.2 carries no ``cold_temperature_projection``, so each cycle's one
read must fail closed with ``PROJECTION_KEY_MISSING`` and return no
observation.  The cycle then calls ``client.finalise_session`` directly.  That
is a direct-client call after a direct-client error; it is not, and is not
evidence about, finalisation after a retained engine abort, which the engine
never performs (proved by the engine's own T7/T16 real-client cases).
"""

# pyright: reportPrivateUsage=false

import asyncio
import importlib.metadata
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import two_phase
from roastpilot_agent.cold_characterisation.evidence_schema import ColdPhaseKind
from roastpilot_agent.cold_characterisation.mcp import (
    ColdCharacterisationMCPClient,
    ColdTickTemperatureProjectionError,
    SessionFinalisationResult,
    finalisation_has_required_safety_evidence,
    finalisation_is_clean,
)
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTemperatureProjectionFailure,
)
from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig, MCPDeviceConfig
from roastpilot_agent.mcp_client import MCPServerProcess, resolve_mcp_command

OFF = ColdPhaseKind.RECORDING_OFF
ON = ColdPhaseKind.RECORDING_ON
_YAML = """roaster:
  driver: mock
first_crack:
  mode: disabled
ambient:
  mode: disabled
"""

pytestmark = [
    pytest.mark.slow,
    pytest.mark.serial(reason="drives a real MCP child and verifies its teardown"),
]


class _TmpYamlChild:
    """Test-owned child adapter: one process, two pre-bound temporary-yaml configs.

    ``configure_phase`` only selects the configuration rendered on the next spawn;
    it makes no MCP call.  The production adapter is deferred to slice 6.
    """

    def __init__(
        self, process: MCPServerProcess, configs: dict[ColdPhaseKind, MCPDeviceConfig]
    ) -> None:
        self._process = process
        self._configs = configs

    async def start(self) -> None:
        await self._process.start()

    async def stop(self) -> None:
        await self._process.stop()

    def configure_phase(self, phase: ColdPhaseKind) -> None:
        self._process.set_device_config(self._configs[phase])

    @property
    def running(self) -> bool:
        return self._process.running

    @property
    def stop_unconfirmed(self) -> bool:
        return self._process.stop_unconfirmed


def _device_config(source: Path, recording: bool) -> MCPDeviceConfig:
    """One phase configuration explicitly bound to the temporary yaml."""
    return MCPDeviceConfig(
        mcp_yaml_source_path=source,
        roaster_driver="mock",
        fc_mode="disabled",
        ambient_mode="disabled",
        recording_enabled=recording,
        recording_autocapture=recording,
    )


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_t_rc_one_process_and_client_across_tasks_finalise_clean_and_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    record_property: Callable[[str, object], None],
) -> None:
    """T-RC: OFF and ON cycles finalise clean and both children stop confirmed."""
    assert importlib.metadata.version("coffee-roaster-mcp") == "0.2.2"
    assert Path(resolve_mcp_command(DEFAULT_MCP_COMMAND)).is_file()
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("COFFEE_"):
            monkeypatch.delenv(name)
    tmp_yaml = tmp_path / "coffee-roaster-mcp.yaml"
    tmp_yaml.write_text(_YAML, encoding="utf-8")
    process = MCPServerProcess(
        MCPConfig(
            env={
                "COFFEE_ROASTER_MCP_CONFIG": str(tmp_yaml),
                "COFFEE_ROASTER_DRIVER": "mock",
                "COFFEE_FIRST_CRACK_MODE": "disabled",
                "COFFEE_AMBIENT_MODE": "disabled",
            },
            startup_timeout_seconds=15.0,
            call_timeout_seconds=5.0,
            stop_timeout_seconds=10.0,
        )
    )
    pids: list[int] = []
    register = process._register_force_terminate

    def capture(pid: int) -> None:
        pids.append(pid)
        register(pid)

    monkeypatch.setattr(process, "_register_force_terminate", capture)
    adapter = _TmpYamlChild(
        process, {OFF: _device_config(tmp_yaml, False), ON: _device_config(tmp_yaml, True)}
    )
    port: two_phase.ColdChildLifecycle = adapter
    owner = two_phase._ChildOwner(port)
    client = ColdCharacterisationMCPClient(process.call_tool)
    results: dict[ColdPhaseKind, SessionFinalisationResult] = {}
    caplog.set_level(logging.DEBUG)

    async def cycle(phase: ColdPhaseKind) -> None:
        assert owner.configure(phase) is True
        assert await owner.start() is True
        assert owner.state is two_phase._ChildState.RUNNING_CONFIRMED
        runtime = await client.get_runtime_config()
        assert runtime.first_crack_mode == "disabled"
        assert runtime.roaster_driver == "mock"
        started = await client.start_cold_session()
        session = started.session.session_id
        await client.mark_beans_added()
        with pytest.raises(ColdTickTemperatureProjectionError) as raised:
            await client.get_roast_state()
        assert raised.value.failure is ColdTemperatureProjectionFailure.PROJECTION_KEY_MISSING
        # Direct-client finalisation after a direct-client error, not an engine sequence.
        results[phase] = await client.finalise_session(session)

    async def task_a() -> None:
        await cycle(OFF)

    async def task_b() -> None:
        await owner.stop()
        assert owner.state is two_phase._ChildState.STOPPED_CONFIRMED
        await cycle(ON)

    async def task_c() -> None:
        await owner.stop()

    try:
        await asyncio.create_task(task_a())
        await asyncio.create_task(task_b())
        await asyncio.create_task(task_c())
    finally:
        if process.running:
            await asyncio.wait_for(process.stop(), 30.0)
    assert owner.state is two_phase._ChildState.STOPPED_CONFIRMED
    assert owner.ownership is two_phase.ColdChildOwnership.OWNED_STOP_CONFIRMED
    assert process.stop_unconfirmed is False and process.running is False
    assert len(pids) == 2 and pids[0] != pids[1]
    for _ in range(50):
        if all(_pid_is_gone(pid) for pid in pids):
            break
        await asyncio.sleep(0.1)
    assert all(_pid_is_gone(pid) for pid in pids)
    off, on = results[OFF], results[ON]
    assert off.session_id != on.session_id
    for result in (off, on):
        assert result.status == "clean" and result.clean is True
        assert finalisation_is_clean(result)
        assert finalisation_has_required_safety_evidence(result)
        assert result.session_purpose == "cold_characterisation"
        assert result.first_crack_runtime is not None
        assert result.first_crack_runtime.outcome == "not_active"
    assert off.recording is not None
    assert (off.recording.expected, off.recording.outcome) == (False, "not_configured")
    assert on.recording is not None
    # Observed with 0.2.2 and first crack disabled: the rendered recording-on flags
    # are inert, so this is never recording-on acceptance.
    assert (on.recording.expected, on.recording.outcome) == (False, "not_configured")
    record_property("pids", tuple(pids))
    record_property(
        "statuses",
        tuple(
            (r.status, r.recording.expected, r.recording.outcome) for r in (off, on) if r.recording
        ),
    )
    messages = " ".join(record.getMessage().lower() for record in caplog.records)
    assert "cancel scope" not in messages and "different task" not in messages
