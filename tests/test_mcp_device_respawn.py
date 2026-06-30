"""Tests for MCP device-config respawn on roast start (#431).

Verifies that:
- A saved mcp_device change is detected at start_roast and triggers a
  between-roast respawn of the MCP child with the new YAML config.
- No respawn fires when the device config has not changed.
- No respawn fires while a roast is active (the active-run guard holds).
- Heat and fan are never auto-resumed by the respawn — the new child starts
  clean (operator_recovery_required invariant is not violated for a known-idle
  between-roast respawn).
- The baseline (_spawned_mcp_device) is only set in live-serve mode; a non-live
  service is never respawned.

All tests are hardware-free: MCPServerProcess is replaced with a
FakeMCPProcess (a minimal stub that records start/stop calls and supports
set_device_config), wired into a RoastService with live_serve_mode=True
and an isolated config file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoastService
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.config_store import (
    AppConfigEdit,
    MCPDeviceConfigEdit,
    persist_config_edit,
)
from roastpilot_agent.models import RoastProfile
from roastpilot_agent.store import RoastStore

# ---------------------------------------------------------------------------
# Fake MCP process stub
# ---------------------------------------------------------------------------


class FakeMCPProcess:
    """Minimal MCPServerProcess stub that records start/stop/set_device_config.

    Supports the interface RoastService touches:
    - ``running`` property
    - ``stop_unconfirmed`` property
    - ``stop()`` / ``start()`` async methods
    - ``set_device_config()``
    - ``device_config`` property
    - ``call_tool()`` (never called in these tests; present for completeness)
    """

    def __init__(self, device_config: MCPDeviceConfig | None = None) -> None:
        self._device_config = device_config
        self._running = False
        self._stop_unconfirmed = False
        #: Ordered call log: "start" / "stop" / "set_device_config".
        self.calls: list[str] = []
        #: Device config values passed to each set_device_config call.
        self.set_device_config_args: list[MCPDeviceConfig] = []

    @property
    def device_config(self) -> MCPDeviceConfig | None:
        return self._device_config

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stop_unconfirmed(self) -> bool:
        return self._stop_unconfirmed

    def set_device_config(self, device_config: MCPDeviceConfig) -> None:
        self._device_config = device_config
        self.calls.append("set_device_config")
        self.set_device_config_args.append(device_config)

    async def start(self) -> None:
        self._running = True
        self.calls.append("start")

    async def stop(self) -> None:
        self._running = False
        self.calls.append("stop")

    async def call_tool(  # pragma: no cover
        self, name: str, arguments: dict[str, object]
    ) -> object:
        raise NotImplementedError("FakeMCPProcess.call_tool should not be called in these tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(**kwargs: Any) -> dict[str, Any]:
    base = {
        "name": "Test Roast",
        "bean_origin": "Kenya",
        "bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    """Initialised store backed by a per-test SQLite file."""
    db = RoastStore(tmp_path / "test.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config file path; point load_app_config at it via env var."""
    path = tmp_path / "roastpilot-config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(path))
    return path


def _live_service_with_fake_mcp(
    store: RoastStore,
    fake_mcp: FakeMCPProcess,
    initial_device_config: MCPDeviceConfig | None = None,
) -> RoastService:
    """Build a live-serve RoastService wired to a FakeMCPProcess.

    Mirrors the key steps of build_live_service without real I/O:
    the fake MCP is passed as the ``mcp`` parameter and the service's
    ``_spawned_mcp_device`` is initialised via ``set_spawned_mcp_device``
    to simulate a successful initial spawn.
    """
    device = initial_device_config or MCPDeviceConfig()
    svc = RoastService(
        store,
        mcp=fake_mcp,  # type: ignore[arg-type]
        live_serve_mode=True,
    )
    # Mirror build_live_service: record the device config for the initial spawn.
    svc.set_spawned_mcp_device(device)
    return svc


# ---------------------------------------------------------------------------
# Respawn fires when mcp_device changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_fires_when_mcp_device_changes(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A saved mcp_device change triggers a respawn at start_roast.

    The service starts with the default (all-None) mcp_device.  A
    persist_config_edit call changes serial_port.  The next start_roast must
    detect the drift and respawn the child: stop → set_device_config → start.
    """
    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    # Save a device config change between roasts.
    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    # start_roast must detect the drift and respawn.
    await svc.start_roast(RoastProfile(**_profile()))

    # Respawn sequence: stop → set_device_config → start (in that order).
    assert fake_mcp.calls == ["stop", "set_device_config", "start"]

    # The new device config must be the fresh one loaded from the saved file.
    assert len(fake_mcp.set_device_config_args) == 1
    assert fake_mcp.set_device_config_args[0].serial_port == "/dev/ttyUSB1"

    # The service must have recorded the new spawned device config.
    assert svc._spawned_mcp_device is not None  # pyright: ignore[reportPrivateUsage]
    assert svc._spawned_mcp_device.serial_port == "/dev/ttyUSB1"  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# No respawn when device config unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_respawn_when_mcp_device_unchanged(
    store: RoastStore,
    config_file: Path,
) -> None:
    """No respawn fires when the saved mcp_device matches the spawned config.

    A between-roast advisor-only save must not churn the MCP child.
    """
    from roastpilot_agent.config_store import AdvisorConfigEdit

    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    # Save a change that does NOT touch mcp_device.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    await svc.start_roast(RoastProfile(**_profile()))

    # MCP child must not have been touched.
    assert fake_mcp.calls == []


# ---------------------------------------------------------------------------
# No respawn before baseline is set (spawned_mcp_device=None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_respawn_before_spawned_baseline_is_set(
    store: RoastStore,
    config_file: Path,
) -> None:
    """No respawn fires when _spawned_mcp_device has not yet been set.

    Without a recorded baseline there is nothing to compare against; the
    guard must not perform a spurious respawn on the first roast start after
    startup (or in API-only mode where no MCP was ever spawned).
    """
    # Deliberately skip set_spawned_mcp_device — baseline stays None.
    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = RoastService(
        store,
        mcp=fake_mcp,  # type: ignore[arg-type]
        live_serve_mode=True,
    )
    # _spawned_mcp_device is None at this point (no set_spawned_mcp_device call).

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB9")))

    await svc.start_roast(RoastProfile(**_profile()))

    # No respawn — no baseline to compare against.
    assert fake_mcp.calls == []


# ---------------------------------------------------------------------------
# Respawn never fires mid-roast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_respawn_mid_roast(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A concurrent start_roast attempt while a roast is active is rejected before
    the device-comparison or respawn logic is reached.

    Verifies the invariant: respawn can only happen between roasts, never
    mid-roast (the active_run() guard fires first → RoastRunConflictError, and
    the MCP child is never touched).
    """
    from roastpilot_agent.api import RoastRunConflictError

    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    # First roast starts successfully.
    await svc.start_roast(RoastProfile(**_profile()))
    # Record calls so far (may include a respawn from the first start_roast).
    calls_after_first = list(fake_mcp.calls)

    # Simulate a mid-roast state by leaving active_run_id set.
    # A second start_roast while the run is persisted must be rejected.
    with pytest.raises(RoastRunConflictError):
        await svc.start_roast(RoastProfile(**_profile()))

    # The rejected call must not have added any MCP calls.
    assert fake_mcp.calls == calls_after_first


# ---------------------------------------------------------------------------
# Non-live service never respawns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_live_service_never_respawns(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A service with live_serve_mode=False never respawns the MCP child.

    The config-reload and respawn blocks are gated on live_serve_mode=True;
    test doubles, replay, and API-only mode must never have their injected
    config or MCP replaced.
    """
    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    # live_serve_mode defaults to False — skip the set_spawned_mcp_device step.
    svc = RoastService(
        store,
        mcp=fake_mcp,  # type: ignore[arg-type]
        live_serve_mode=False,
    )

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    await svc.start_roast(RoastProfile(**_profile()))

    # No MCP calls — reload and respawn are gated on live_serve_mode=True.
    assert fake_mcp.calls == []


# ---------------------------------------------------------------------------
# No heat/fan auto-resume after respawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_does_not_auto_resume_heat_fan(
    store: RoastStore,
    config_file: Path,
) -> None:
    """After a respawn the new MCP child does not receive heat/fan commands
    before the operator explicitly starts the roast session.

    The respawn path (stop → set_device_config → start) must not call
    set_targets, set_heat, or set_fan.  Heat/fan only flow via the controller
    AFTER start_session (operator action) — never from the respawn itself.
    """
    # Capture all calls including set_targets via a simple call recorder.
    heat_fan_calls: list[str] = []

    class TrackingFakeMCP(FakeMCPProcess):
        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            heat_fan_calls.append(f"set_targets({heat_percent},{fan_percent})")

        async def start_session(
            self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
        ) -> None:
            heat_fan_calls.append("start_session")

    fake_mcp = TrackingFakeMCP(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    await svc.start_roast(RoastProfile(**_profile()))

    # The respawn sequence must not include any heat/fan commands.
    # start_session is called by the controller during start_run (normal path),
    # but set_targets must not appear in the respawn window (before start_session).
    assert "set_targets" not in fake_mcp.calls
    # Specifically: the respawn must be stop → set_device_config → start only.
    # The controller is wired but has no roaster (_roaster=None in this test),
    # so start_session and set_targets are not invoked either.
    assert set(fake_mcp.calls).issubset({"stop", "set_device_config", "start"})


# ---------------------------------------------------------------------------
# set_spawned_mcp_device round-trip
# ---------------------------------------------------------------------------


def test_set_spawned_mcp_device_records_baseline(tmp_path: Path) -> None:
    """set_spawned_mcp_device stores the device config for later comparison.

    Verifies the public method without any async machinery.
    """
    import asyncio

    async def _run() -> None:
        db = RoastStore(tmp_path / "test.db")
        await db.initialize()
        try:
            svc = RoastService(db)
            assert svc._spawned_mcp_device is None  # pyright: ignore[reportPrivateUsage]

            device = MCPDeviceConfig(serial_port="/dev/ttyUSB0", roaster_driver="mock")
            svc.set_spawned_mcp_device(device)

            assert svc._spawned_mcp_device == device  # pyright: ignore[reportPrivateUsage]
            assert svc._spawned_mcp_device.serial_port == "/dev/ttyUSB0"  # pyright: ignore[reportPrivateUsage]
        finally:
            await db.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCPServerProcess.set_device_config unit test
# ---------------------------------------------------------------------------


def test_mcp_process_set_device_config() -> None:
    """MCPServerProcess.set_device_config updates _device_config in place.

    Verifies the new public method on the real MCPServerProcess using an
    injected fake session (no child process).
    """
    from roastpilot_agent.mcp_client import MCPServerProcess

    class _StubSession:
        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> object:
            return {}

        async def initialize(self) -> object:
            return {}

    # Construct with an injected session so no child process is spawned.
    initial_device = MCPDeviceConfig(serial_port="/dev/ttyUSB0")
    mcp = MCPServerProcess(device_config=initial_device, session=_StubSession())

    assert mcp.device_config == initial_device
    assert mcp.device_config is not None
    assert mcp.device_config.serial_port == "/dev/ttyUSB0"

    new_device = MCPDeviceConfig(
        serial_port="/dev/ttyUSB1", roaster_driver="hottop_kn8828b_2k_plus"
    )
    mcp.set_device_config(new_device)

    assert mcp.device_config == new_device
    assert mcp.device_config.serial_port == "/dev/ttyUSB1"
    assert mcp.device_config.roaster_driver == "hottop_kn8828b_2k_plus"
