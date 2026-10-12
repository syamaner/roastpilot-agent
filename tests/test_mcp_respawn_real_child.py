"""Real-child respawn tests — the cross-task cancel-scope gap (#484).

The device-config respawn path was only ever exercised against a
``FakeMCPProcess`` (``test_mcp_device_respawn.py``), so the anyio same-task
invariant was never tested against the real stdio stack: ``stdio_client`` /
``ClientSession`` enter their cancel scopes in ONE task, and exiting them from a
DIFFERENT task raises
``Attempted to exit cancel scope in a different task than it was entered in``.

In production this bit at ``start_roast``: the MCP child was spawned in the
serve/lifespan task, but a between-roast respawn stopped it from the
request-handler task — a different task. So these tests spawn the **real**
``coffee-roaster-mcp`` in **mock-driver mode** (hardware-free per AGENTS.md) and
drive the stop/respawn from a task **other than** the one that started the child,
which is exactly the shape the fake could not reproduce.

Assertions per the #484 acceptance:
- the respawn (stop → set_device_config → start) from a foreign task raises no
  exception (the pre-fix crash);
- the OLD child pid is fully terminated — no orphan holding the serial/audio
  device;
- the NEW child is healthy and answers a typed call;
- a SECOND respawn also works (recycled-pid hygiene, cross-refs #431).

The API service test drives the same between-roast respawn primitive in the
request-handler task.  D213 blocks a fresh process from ``start_roast`` until
it has durable operator clearance and typed no-session proof, so this test
cannot use start as its respawn trigger.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoastService
from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig, MCPDeviceConfig
from roastpilot_agent.config_store import (
    AppConfigEdit,
    MCPDeviceConfigEdit,
    persist_config_edit,
)
from roastpilot_agent.mcp_client import (
    MCPServerProcess,
    RoasterControlAdapter,
    RoasterMCPClient,
    resolve_mcp_command,
)
from roastpilot_agent.models import RestartClearanceRequest, RoastPhase, RoastProfile
from roastpilot_agent.store import RoastStore

# The spawn resolves the default command to the in-venv console script
# (resolve_mcp_command), so skip only when that resolved binary is missing —
# the same gate test_real_child_process_round_trip uses.
pytestmark = [
    pytest.mark.skipif(
        not os.path.isfile(resolve_mcp_command(DEFAULT_MCP_COMMAND)),
        reason="coffee-roaster-mcp not installed where the spawn resolves it",
    ),
    pytest.mark.serial(reason="drives a real MCP child and verifies process teardown"),
    # Spawns a real subprocess and initialises an MCP session twice: slower than
    # a unit test, so tag it for opt-out (still runs hardware-free in CI).
    pytest.mark.slow,
]

# Bootstrap-safe mock config: no Hottop, no microphone, no model download.
_MOCK_CONFIG_YAML = """
roaster:
  driver: mock
first_crack:
  mode: disabled
"""

#: The exact anyio failure the pre-fix cross-task stop logged at ERROR (and
#: swallowed). The fixed owner-task lifecycle must never emit it — asserting its
#: absence is the load-bearing regression signal: on this bare event loop the
#: child dies regardless (the pipes close), so only the log distinguishes a clean
#: in-task teardown from the swallowed cross-task scope crash.
_CROSS_TASK_SCOPE_ERROR = "cancel scope in a different task"


def _assert_no_cross_task_scope_error(caplog: pytest.LogCaptureFixture) -> None:
    """Fail if any captured log records the cross-task cancel-scope crash (#484)."""
    offending = [
        r.getMessage() for r in caplog.records if _CROSS_TASK_SCOPE_ERROR in r.getMessage()
    ]
    assert not offending, f"cross-task cancel-scope error leaked from stop(): {offending}"


def _write_mock_yaml(tmp_path: Path, name: str) -> Path:
    """Write a bootstrap-safe mock-driver MCP yaml and return its path."""
    path = tmp_path / name
    path.write_text(_MOCK_CONFIG_YAML, encoding="utf-8")
    return path


def _pid_alive(pid: int) -> bool:
    """True while the process (group leader) is still alive (POSIX)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours (never here)
        return True
    return True


async def _wait_pid_gone(pid: int, *, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is gone, up to ``timeout`` seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _pid_alive(pid)


class _PidCapturingMCP(MCPServerProcess):
    """MCPServerProcess that records each spawned child's pid.

    The default factory hands the spawned pid to ``_register_force_terminate``;
    we tee that call so the test can assert the OLD pid is reaped after a
    respawn (no orphan) without reaching into private transport internals.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.spawned_pids: list[int] = []

    def _register_force_terminate(self, pid: int) -> None:
        self.spawned_pids.append(pid)
        super()._register_force_terminate(pid)


class _RecordingRoasterControlAdapter(RoasterControlAdapter):
    """Adapter that records the order of session and control writes."""

    def __init__(self, client: RoasterMCPClient) -> None:
        super().__init__(client)
        self.command_order: list[str] = []

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        """Record the session boundary before forwarding the typed MCP command."""
        self.command_order.append("start_session")
        await super().start_session(
            recording_origin=recording_origin, recording_roast_num=recording_roast_num
        )

    async def set_heat(self, heat_percent: int) -> None:
        """Record one heat write before forwarding it."""
        self.command_order.append("set_heat")
        await super().set_heat(heat_percent)

    async def set_fan(self, fan_percent: int) -> None:
        """Record one fan write before forwarding it."""
        self.command_order.append("set_fan")
        await super().set_fan(fan_percent)

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        """Record combined target writes so they cannot hide auto-resume."""
        self.command_order.append("set_targets")
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    db = RoastStore(tmp_path / "respawn-real.sqlite3")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "roastpilot-config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(path))
    return path


@pytest.mark.asyncio
async def test_cross_task_respawn_no_scope_crash_and_no_orphan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stop+restart the real child from a task OTHER than the one that started it.

    This is the exact cross-task shape #484 crashed on. Pre-fix, the stop's
    ``aclose`` exited the stdio cancel scope in the wrong task and logged
    ``Attempted to exit cancel scope in a different task`` (swallowed). Post-fix
    the owner task exits the scope in-task, so the respawn logs no such error,
    the old child pid is reaped (no orphan), the new child answers, and a SECOND
    respawn also works.
    """
    caplog.set_level(logging.ERROR, logger="roastpilot_agent.mcp_client")
    yaml_path = _write_mock_yaml(tmp_path, "mock.yaml")
    process = _PidCapturingMCP(MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(yaml_path)}))

    # Start in a DEDICATED task — mimics the serve/lifespan task in production,
    # which is a different task from the request handler that later respawns.
    await asyncio.create_task(process.start())
    try:
        assert process.running
        assert len(process.spawned_pids) == 1
        first_pid = process.spawned_pids[0]
        assert _pid_alive(first_pid)

        # The child answers a typed call before the respawn.
        info = await RoasterMCPClient(process.call_tool).get_server_info()
        assert info.bootstrap_safe is True
        assert info.roaster_driver == "mock"

        # --- Respawn from a FOREIGN task (the crash trigger). ---
        async def _respawn() -> None:
            await process.stop()
            await process.start()

        # No exception may leak: pre-fix this raised the cancel-scope error.
        await asyncio.create_task(_respawn())

        assert process.running
        # A clean stop must never have flagged an unconfirmed teardown.
        assert process.stop_unconfirmed is False
        # The OLD child must be fully gone — no orphan holding the device.
        assert await _wait_pid_gone(first_pid), (
            f"old MCP child pid {first_pid} survived the respawn (orphan)"
        )

        # The NEW child is a distinct, live process that answers.
        assert len(process.spawned_pids) == 2
        second_pid = process.spawned_pids[1]
        assert second_pid != first_pid
        assert _pid_alive(second_pid)
        info2 = await RoasterMCPClient(process.call_tool).get_server_info()
        assert info2.bootstrap_safe is True

        # --- A SECOND respawn also works (recycled-pid hygiene, #431). ---
        await asyncio.create_task(_respawn())
        assert process.running
        assert process.stop_unconfirmed is False
        assert await _wait_pid_gone(second_pid), (
            f"second MCP child pid {second_pid} survived the respawn (orphan)"
        )
        assert len(process.spawned_pids) == 3
        third_pid = process.spawned_pids[2]
        assert _pid_alive(third_pid)
    finally:
        # Stop from yet another task; must also be clean.
        await asyncio.create_task(process.stop())
        assert not process.running
        # No leaked children.
        for pid in process.spawned_pids:
            assert await _wait_pid_gone(pid), f"MCP child pid {pid} leaked after final stop"
    # The core #484 assertion: every stop ran its aclose in the owner task, so
    # the cross-task cancel-scope error was never logged.
    _assert_no_cross_task_scope_error(caplog)


@pytest.mark.asyncio
async def test_service_respawn_real_child_start_does_not_auto_resume_heat_fan(
    store: RoastStore,
    config_file: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D213 clearance plus Start respawns a real child without heat/fan resume.

    The initial mock child supplies its typed 0.2.3 no-session snapshot before
    the operator's explicit physical confirmation. A saved device change then
    makes the actual Start path stop and replace that child. Normal Start
    targets may only follow the new-session command: respawn never restores
    heat or fan from an earlier process.
    """
    caplog.set_level(logging.ERROR, logger="roastpilot_agent.mcp_client")
    yaml_path = _write_mock_yaml(tmp_path, "mock.yaml")
    initial_device = MCPDeviceConfig()  # spawned WITHOUT ambient (matches the bug)
    process = _PidCapturingMCP(
        MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(yaml_path)}),
        device_config=initial_device,
    )
    # Start the child in a dedicated task (serve/lifespan task).
    await asyncio.create_task(process.start())
    try:
        first_pid = process.spawned_pids[0]
        adapter = _RecordingRoasterControlAdapter(RoasterMCPClient(process.call_tool))
        service = RoastService(
            store,
            mcp=process,
            roaster=adapter,
            exporter=adapter,
            raw_state=adapter,
            live_serve_mode=True,
            run_loop=False,
        )
        service.set_spawned_mcp_device(initial_device)

        assert await adapter.read_session_presence() == "none"
        clearance = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert clearance.cleared
        assert adapter.command_order == []

        persist_config_edit(
            AppConfigEdit(
                mcp_device=MCPDeviceConfigEdit(
                    ambient_mode="yoctopuce", ambient_device="METEOMK2-1"
                )
            )
        )
        detail = await asyncio.create_task(
            service.start_roast(
                RoastProfile(
                    name="Mock respawn roast",
                    bean_origin="Test origin",
                    bean_weight_grams=250,
                    initial_heat_percent=70,
                    initial_fan_percent=40,
                    target_drop_temp_c=195,
                    target_development_percent=14,
                )
            )
        )

        # Clean respawn and admitted Start — no 500 cascade.
        assert service.mcp_child_status().value == "running"
        assert process.stop_unconfirmed is False

        # The child was actually respawned with the new device config.
        assert await _wait_pid_gone(first_pid), "old child survived the start_roast respawn"
        assert len(process.spawned_pids) == 2
        new_pid = process.spawned_pids[1]
        assert _pid_alive(new_pid)

        # A respawn emitted no heat/fan write before the new session boundary.
        # Any normal preheat target belongs to this explicit Start, never to an
        # automatic restoration by child replacement.
        start_index = adapter.command_order.index("start_session")
        assert start_index == 0
        assert all(
            index > start_index
            for index, name in enumerate(adapter.command_order)
            if name in {"set_heat", "set_fan", "set_targets"}
        )

        # The Start response and persisted run agree on the explicit new session.
        active = await store.active_run()
        assert active is not None
        assert active.run_id == detail.id
        assert detail.agent_phase is RoastPhase.PREHEATING
        assert adapter.command_order == ["start_session", "set_heat", "set_fan"]
    finally:
        await asyncio.create_task(process.stop())
        for pid in process.spawned_pids:
            assert await _wait_pid_gone(pid), f"MCP child pid {pid} leaked"
    # The respawn's cross-task stop must not have logged the scope crash (#484).
    _assert_no_cross_task_scope_error(caplog)
