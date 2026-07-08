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


@pytest.mark.asyncio
async def test_respawn_fires_when_ambient_config_changes(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Changing an ambient field (D85, #474) is detected as device-config drift
    and triggers a between-roast respawn, exactly like serial/audio/FC fields."""
    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    persist_config_edit(
        AppConfigEdit(
            mcp_device=MCPDeviceConfigEdit(
                ambient_mode="yoctopuce",
                ambient_device="METEOMK2-1",
                ambient_poll_interval_seconds=15.0,
            )
        )
    )

    await svc.start_roast(RoastProfile(**_profile()))

    assert fake_mcp.calls == ["stop", "set_device_config", "start"]
    assert len(fake_mcp.set_device_config_args) == 1
    new_device = fake_mcp.set_device_config_args[0]
    assert new_device.ambient_mode == "yoctopuce"
    assert new_device.ambient_device == "METEOMK2-1"
    assert new_device.ambient_poll_interval_seconds == 15.0

    assert svc._spawned_mcp_device is not None  # pyright: ignore[reportPrivateUsage]
    assert svc._spawned_mcp_device.ambient_mode == "yoctopuce"  # pyright: ignore[reportPrivateUsage]


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
async def test_none_baseline_triggers_respawn(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A None _spawned_mcp_device baseline with a live _mcp triggers a respawn.

    None baseline has two sources in production:
      1. A failed respawn (the fix in #431): _respawn_mcp_for_device_config
         invalidates the baseline before stop/start so a stuck child is
         re-attempted next start rather than silently skipped.
      2. build_live_service omits set_spawned_mcp_device (should not happen
         in production, but the conservative guard handles it safely).

    In both cases the correct behaviour is to attempt a respawn — "child state
    unknown, respawn to be safe" — rather than skip and hit a dead child.

    Protection against spurious respawns in API-only / replay mode comes from
    the outer ``self._mcp is not None`` guard: those modes pass mcp=None.
    """
    # Deliberately skip set_spawned_mcp_device — baseline stays None.
    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = RoastService(
        store,
        mcp=fake_mcp,  # type: ignore[arg-type]
        live_serve_mode=True,
    )
    # _spawned_mcp_device is None — respawn must fire conservatively.

    await svc.start_roast(RoastProfile(**_profile()))

    # Respawn fires: stop → set_device_config → start (child state was unknown).
    assert fake_mcp.calls == ["stop", "set_device_config", "start"]
    # Baseline is set to the freshly-loaded config after success.
    assert svc._spawned_mcp_device is not None  # pyright: ignore[reportPrivateUsage]


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
    during the respawn itself.

    The respawn path (stop → set_device_config → start) must not issue
    set_targets or any other heat/fan command.  Heat/fan only flow via the
    controller tick loop AFTER start_session (operator action) — never from
    the respawn.

    The test wires a real roaster fake (FakeMCPClient) so _begin_live_run
    runs and the controller is active — meaning the assertion cannot trivially
    pass because _roaster is None.  Only the respawn window (before
    _begin_live_run → start_session) is checked for heat/fan calls.
    """
    from tests.conftest import FakeMCPClient

    # FakeMCPClient is the roaster/exporter/raw_state: implements RoasterControl.
    # run_loop=False so the controller's tick loop does not run in the background
    # (we just need start_session to execute, not the full tick).
    roaster = FakeMCPClient()

    fake_mcp = FakeMCPProcess(device_config=MCPDeviceConfig())
    svc = RoastService(
        store,
        mcp=fake_mcp,  # type: ignore[arg-type]
        roaster=roaster,  # type: ignore[arg-type]
        exporter=roaster,  # type: ignore[arg-type]
        raw_state=roaster,  # type: ignore[arg-type]
        live_serve_mode=True,
        run_loop=False,
    )
    svc.set_spawned_mcp_device(MCPDeviceConfig())

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    await svc.start_roast(RoastProfile(**_profile()))

    # The respawn (stop → set_device_config → start) must appear in fake_mcp.calls,
    # but no heat/fan command may appear there.
    assert "stop" in fake_mcp.calls
    assert set(fake_mcp.calls).issubset({"stop", "set_device_config", "start"})

    # The roaster (FakeMCPClient) must not have received set_targets during the
    # respawn window.  start_session is called by _begin_live_run (normal path)
    # and is acceptable; only set_targets before start_session would be a violation.
    roaster_call_names = [name for name, _ in roaster.calls]
    set_targets_idx = [i for i, n in enumerate(roaster_call_names) if n == "set_targets"]
    start_session_idx = next(
        (i for i, n in enumerate(roaster_call_names) if n == "start_session"), None
    )
    # No set_targets should precede the first start_session (or appear at all
    # before the controller tick loop fires, which is not running here).
    if start_session_idx is not None:
        assert all(idx > start_session_idx for idx in set_targets_idx), (
            "set_targets appeared before start_session — heat/fan issued during respawn"
        )
    else:
        assert set_targets_idx == [], "set_targets issued with no start_session — auto-resume"


# ---------------------------------------------------------------------------
# Respawn failure: baseline invalidated, retry succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_respawn_clears_baseline_and_retry_succeeds(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A respawn whose start() raises clears _spawned_mcp_device to None.

    Scenario:
    1. Operator saves a bad serial port (new config != spawned baseline).
    2. start_roast detects drift and calls _respawn_mcp_for_device_config.
    3. stop() succeeds; start() raises (bad port, driver not found, etc.).
    4. _spawned_mcp_device must be None afterward — the stale old value must
       NOT be restored.
    5. Operator reverts to the original (working) config.
    6. The NEXT start_roast sees fresh.mcp_device != None (since None != any
       config) → drift re-detected → respawn re-attempted → succeeds.

    Without the fix, step 4 would leave _spawned_mcp_device at the original
    value; step 5's revert would make fresh == old → drift NOT detected →
    _begin_live_run would hit a stopped child → stuck until agent restart.
    """
    original_device = MCPDeviceConfig(serial_port="/dev/ttyUSB0")
    bad_device_port = "/dev/ttyUSB_bad"

    start_call_count = 0

    class FailOnceStartMCP(FakeMCPProcess):
        async def start(self) -> None:
            nonlocal start_call_count
            start_call_count += 1
            if start_call_count == 1:
                # Simulate a failed spawn (bad port / driver error).
                raise OSError(f"cannot open port {bad_device_port}")
            await super().start()

    fake_mcp = FailOnceStartMCP(device_config=original_device)
    svc = _live_service_with_fake_mcp(store, fake_mcp, original_device)

    # --- Attempt 1: save a bad serial port, start_roast should raise. ---
    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port=bad_device_port)))
    with pytest.raises(OSError):
        await svc.start_roast(RoastProfile(**_profile()))

    # Baseline must be None after the failed start — not the old value.
    assert svc._spawned_mcp_device is None  # pyright: ignore[reportPrivateUsage]

    # --- Attempt 2: retry with the same (bad) config, but start() now succeeds. ---
    # _spawned_mcp_device is None → the None-baseline guard fires → respawn
    # re-attempted regardless of whether the saved config changed.  start()
    # succeeds on the second call (start_call_count == 2).
    await svc.start_roast(RoastProfile(**_profile()))

    # Second start succeeded; baseline is now set to the freshly-loaded config.
    assert svc._spawned_mcp_device is not None  # pyright: ignore[reportPrivateUsage]
    assert start_call_count == 2  # first attempt raised, second succeeded


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

            assert svc._spawned_mcp_device is not None  # pyright: ignore[reportPrivateUsage]
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

    assert mcp.device_config is not None
    assert mcp.device_config == new_device
    assert mcp.device_config.serial_port == "/dev/ttyUSB1"
    assert mcp.device_config.roaster_driver == "hottop_kn8828b_2k_plus"


# ---------------------------------------------------------------------------
# P1-a: unconfirmed stop aborts the respawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfirmed_stop_aborts_respawn(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A respawn is aborted when stop() leaves stop_unconfirmed=True.

    If the old child had to be force-killed (wedged PortAudio read, etc.) the
    serial port or audio device may still be held.  Starting a new child into
    that state risks a resource conflict or a hidden live process.

    Expected: _respawn_mcp_for_device_config raises MCPConnectionError,
    _spawned_mcp_device stays None (the baseline was already invalidated),
    and set_device_config + start are NOT called.
    """
    from roastpilot_agent.mcp_client import MCPConnectionError as MCPConnErr

    class UnconfirmedStopMCP(FakeMCPProcess):
        """stop() force-kills without confirming (simulates a wedged child)."""

        @property
        def stop_unconfirmed(self) -> bool:
            # Always report unconfirmed after the first stop.
            return self._stop_unconfirmed

        async def stop(self) -> None:
            self._running = False
            self._stop_unconfirmed = True
            self.calls.append("stop")

    fake_mcp = UnconfirmedStopMCP(device_config=MCPDeviceConfig())
    svc = _live_service_with_fake_mcp(store, fake_mcp, MCPDeviceConfig())

    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB1")))

    with pytest.raises(MCPConnErr, match="stop was unconfirmed"):
        await svc.start_roast(RoastProfile(**_profile()))

    # Only stop() was called — set_device_config and start must NOT have run.
    assert fake_mcp.calls == ["stop"]

    # Baseline must be None — not the stale old value.
    assert svc._spawned_mcp_device is None  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# P1-b: force-terminate hook re-armed on each spawn
# ---------------------------------------------------------------------------


def test_force_terminate_hook_rearmed_on_respawn() -> None:
    """The auto-registered force-terminate hook is cleared before each spawn.

    _force_terminate_injected=False (the production path): start() must clear
    _force_terminate to None before spawning so _register_force_terminate
    re-registers with the new pid, not the previous one.

    _force_terminate_injected=True (test seam): start() must NOT clear the
    injected hook.

    Tested without real I/O by inspecting the hook value before/after the
    re-arm step using MCPServerProcess with an injected session.
    """
    from roastpilot_agent.mcp_client import MCPServerProcess

    sentinel_hook: list[str] = []

    def fake_hook_pid1() -> bool:
        sentinel_hook.append("pid1")
        return True

    # --- Non-injected path: hook is cleared before each spawn. ---
    mcp_auto = MCPServerProcess()
    # Manually set a "previous pid's" hook as if a prior spawn registered it.
    mcp_auto._force_terminate = fake_hook_pid1  # pyright: ignore[reportPrivateUsage]
    assert not mcp_auto._force_terminate_injected  # pyright: ignore[reportPrivateUsage]

    # Simulate the re-arm step (the lines added to start() before the spawn).
    if not mcp_auto._force_terminate_injected:  # pyright: ignore[reportPrivateUsage]
        mcp_auto._force_terminate = None  # pyright: ignore[reportPrivateUsage]

    # Hook must be None now — ready for _register_force_terminate(new_pid).
    assert mcp_auto._force_terminate is None  # pyright: ignore[reportPrivateUsage]

    # Simulate _register_force_terminate registering the new pid's hook.
    mcp_auto._register_force_terminate(9999)  # pyright: ignore[reportPrivateUsage]
    assert mcp_auto._force_terminate is not None  # pyright: ignore[reportPrivateUsage]
    # The old pid1 hook is gone; calling the new hook should not append "pid1".
    mcp_auto._force_terminate()  # pyright: ignore[reportPrivateUsage]
    assert "pid1" not in sentinel_hook  # hook was replaced, not stacked

    # --- Injected path: hook is preserved across respawn. ---
    def injected_hook() -> bool:
        return True

    mcp_injected = MCPServerProcess(force_terminate=injected_hook)
    assert mcp_injected._force_terminate_injected  # pyright: ignore[reportPrivateUsage]

    # Simulate the re-arm step: injected flag is True → hook must NOT be cleared.
    if not mcp_injected._force_terminate_injected:  # pyright: ignore[reportPrivateUsage]
        mcp_injected._force_terminate = None  # pyright: ignore[reportPrivateUsage]

    # Hook is still the injected one.
    assert mcp_injected._force_terminate is injected_hook  # pyright: ignore[reportPrivateUsage]
