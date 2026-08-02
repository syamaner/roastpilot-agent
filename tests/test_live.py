"""Live-serve composition tests (E11-S1 early item; supervised roast #134).

Hardware-free: ``build_live_service`` is exercised with a fake stdio session
injected into the real :class:`MCPServerProcess` via its ``session_factory``
seam (the same seam ``test_mcp_client.py`` uses), so no ``coffee-roaster-mcp``
binary, hardware, or network is touched. The static-mount tests use FastAPI's
synchronous ``TestClient`` over a temporary built-SPA tree.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from roastpilot_agent import live
from roastpilot_agent.api import (
    BufferingEventEmitter,
    EventBroadcaster,
    QueuedOperatorAction,
    RoastRunner,
    RoastService,
    _TickCounter,  # pyright: ignore[reportPrivateUsage]
    create_app,
)
from roastpilot_agent.config import (
    AdvisorConfig,
    AppConfig,
    ControllerConfig,
    MCPConfig,
    MCPDeviceConfig,
)
from roastpilot_agent.controller import ControllerSnapshot, RoastController
from roastpilot_agent.mcp_client import (
    MCPConnectionError,
    MCPServerProcess,
    RoasterControlAdapter,
    RoastSessionState,
)
from roastpilot_agent.models import (
    PostFcHeatAuthorityState,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.post_fc_control import PostFcControlOutput
from roastpilot_agent.store import RoastStore

# Reuse the canned tool fixtures + fake-session doubles from the mcp_client tests.
from tests.test_mcp_client import (
    CANNED,
    SESSION_STATE_PAYLOAD,
    FakeInitializableSession,
    FakeResult,
)

# --- COFFEE_* env forwarding ----------------------------------------------------


def test_forward_coffee_env_populates_mcp_env() -> None:
    """``COFFEE_*`` vars are copied from the environment into config.mcp.env."""
    config = AppConfig()
    env = {
        "COFFEE_ROASTER_DRIVER": "mock",
        "COFFEE_ROASTER_PORT": "/dev/ttyUSB0",
        "PATH": "/usr/bin",
    }
    live.forward_coffee_env(config, env)
    assert config.mcp.env["COFFEE_ROASTER_DRIVER"] == "mock"
    assert config.mcp.env["COFFEE_ROASTER_PORT"] == "/dev/ttyUSB0"
    assert "PATH" not in config.mcp.env  # only COFFEE_* is forwarded


def test_forward_coffee_env_does_not_overwrite_existing() -> None:
    """An explicit config.mcp.env value wins over the environment."""
    config = AppConfig(mcp=MCPConfig(env={"COFFEE_ROASTER_DRIVER": "hottop"}))
    live.forward_coffee_env(config, {"COFFEE_ROASTER_DRIVER": "mock"})
    assert config.mcp.env["COFFEE_ROASTER_DRIVER"] == "hottop"


def test_forward_coffee_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit mapping it reads from os.environ."""
    monkeypatch.setenv("COFFEE_FIRST_CRACK_MODE", "disabled")
    config = AppConfig()
    live.forward_coffee_env(config)
    assert config.mcp.env["COFFEE_FIRST_CRACK_MODE"] == "disabled"


# --- advisor wiring -------------------------------------------------------------


def test_build_advisor_returns_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing API key logs a warning and returns None (advisory-paused),
    never blocking startup."""
    config = AppConfig(advisor=AdvisorConfig(api_key_env="ROASTPILOT_TEST_MISSING_KEY"))
    monkeypatch.delenv("ROASTPILOT_TEST_MISSING_KEY", raising=False)
    with caplog.at_level("WARNING"):
        advisor = live.build_advisor(config)
    assert advisor is None
    assert "advisory-paused" in caplog.text


def test_build_advisor_constructs_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the API key set the PydanticAIAdvisor is constructed."""
    from roastpilot_agent.advisor import PydanticAIAdvisor

    config = AppConfig(
        advisor=AdvisorConfig(provider="anthropic", api_key_env="ROASTPILOT_TEST_KEY")
    )
    monkeypatch.setenv("ROASTPILOT_TEST_KEY", "sk-test")
    advisor = live.build_advisor(config)
    assert isinstance(advisor, PydanticAIAdvisor)


# --- advisor reachability probe (issue #168) ------------------------------------


@pytest.mark.asyncio
async def test_probe_advisor_health_none_is_not_configured() -> None:
    """No advisor (advisory-paused) → NOT_CONFIGURED, never an error."""
    from roastpilot_agent.models import AdvisorHealthStatus

    health = await live.probe_advisor_health(None)
    assert health.status is AdvisorHealthStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_probe_advisor_health_reachable() -> None:
    """A reachable FakeAdvisor surfaces its REACHABLE result through the probe."""
    from roastpilot_agent.advisor import FakeAdvisor
    from roastpilot_agent.models import AdvisorHealthStatus

    health = await live.probe_advisor_health(FakeAdvisor())
    assert health.status is AdvisorHealthStatus.REACHABLE
    assert health.provider == "fake"


@pytest.mark.asyncio
async def test_probe_advisor_health_captures_raised_error() -> None:
    """A healthcheck that raises a provider error becomes UNREACHABLE with the
    error — the probe never propagates the exception (serve must not be blocked)."""
    from roastpilot_agent.advisor import AdvisorProviderError, FakeAdvisor
    from roastpilot_agent.models import AdvisorHealthStatus

    advisor = FakeAdvisor(health=AdvisorProviderError("401 invalid key"))
    health = await live.probe_advisor_health(advisor)
    assert health.status is AdvisorHealthStatus.UNREACHABLE
    assert health.error is not None
    assert "401" in health.error


@pytest.mark.asyncio
async def test_probe_advisor_health_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthcheck that hangs is bounded by the wrapper timeout → UNREACHABLE,
    never a wedge. The wrapper bound is driven low so the test is fast."""
    import asyncio

    from roastpilot_agent.advisor import FakeAdvisor
    from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus

    monkeypatch.setattr(live, "ADVISOR_PROBE_WRAP_TIMEOUT_SECONDS", 0.05)

    class _HangingAdvisor(FakeAdvisor):
        async def healthcheck(self) -> AdvisorHealth:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    health = await asyncio.wait_for(live.probe_advisor_health(_HangingAdvisor()), timeout=1.0)
    assert health.status is AdvisorHealthStatus.UNREACHABLE
    assert health.error is not None


def test_advisor_healthcheck_receives_no_mcp_write_tools() -> None:
    """The advisor protocol exposes only advice, reachability, and the trace
    descriptor — never any MCP write surface (the advisory-only invariant). A
    reviewer-readable assertion that neither the probe capability nor the #167
    decision-trace ``descriptor`` smuggled hardware control onto the advisor.
    ``descriptor`` / ``descriptor_for`` are read-only identity metadata
    (provider/model/prompt) — ``descriptor_for(phase)`` is the same identity with
    the phase-resolved model (#189), still no hardware surface."""
    from roastpilot_agent.advisor import RoastAdvisor

    methods = {name for name in dir(RoastAdvisor) if not name.startswith("_")}
    assert methods == {"descriptor", "descriptor_for", "get_recommendation", "healthcheck"}


# --- build_live_service ---------------------------------------------------------


def _info_session() -> FakeInitializableSession:
    """A fake stdio session whose every tool call returns get_server_info."""
    payload = cast("dict[str, object]", CANNED["get_server_info"])
    return FakeInitializableSession(FakeResult(structuredContent=dict(payload)))


class _GoodFactory:
    """Session factory yielding a healthy fake session (start succeeds)."""

    def __init__(self, session: FakeInitializableSession) -> None:
        self.session = session

    def __call__(self, params: object) -> "_GoodFactory._Ctx":
        return _GoodFactory._Ctx(self.session)

    class _Ctx:
        def __init__(self, session: FakeInitializableSession) -> None:
            self._session = session

        async def __aenter__(self) -> FakeInitializableSession:
            return self._session

        async def __aexit__(self, *exc: object) -> None:
            return None


def _good_process_factory(
    session: FakeInitializableSession,
) -> Callable[..., MCPServerProcess]:
    """A drop-in for ``live.MCPServerProcess`` that injects a healthy fake
    session, so ``build_live_service`` starts/health-checks with no hardware.

    Accepts ``device_config`` (D78-4, #420) via ``**kwargs`` so the factory
    signature stays forward-compatible without needing to replicate every
    ``MCPServerProcess.__init__`` keyword argument.
    """

    def _factory(
        config: MCPConfig, *, device_config: MCPDeviceConfig | None = None, **_kwargs: object
    ) -> MCPServerProcess:  # noqa: E501
        # device_config is intentionally unused here — the yaml-render path is
        # an integration concern; unit tests inject a pre-wired session instead.
        return MCPServerProcess(config, session_factory=_GoodFactory(session))

    return _factory


class _FailingProcess(MCPServerProcess):
    """An MCPServerProcess whose start() always fails, tracking stop() calls."""

    def __init__(
        self, config: MCPConfig | None = None, *, device_config: MCPDeviceConfig | None = None
    ) -> None:
        super().__init__(config, device_config=device_config)
        self.stop_called = False

    async def start(self) -> None:
        raise MCPConnectionError("simulated dead child")

    async def stop(self) -> None:
        self.stop_called = True
        await super().stop()


def _failing_process_factory(
    process: _FailingProcess,
) -> Callable[..., MCPServerProcess]:
    """A drop-in for ``live.MCPServerProcess`` returning the failing double."""

    def _factory(config: MCPConfig, **_kwargs: object) -> MCPServerProcess:
        return process

    return _factory


@pytest.mark.asyncio
async def test_build_live_service_wires_adapter_as_reader_and_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's roaster surface is the RoasterControlAdapter over the MCP
    child (reader + executor are the same adapter, the controller never sees the
    raw client)."""
    # No API key in the environment → the advisory-paused path (advisor is None).
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    session = _info_session()
    monkeypatch.setattr(live, "MCPServerProcess", _good_process_factory(session))
    config = AppConfig()
    service, mcp, store = await live.build_live_service(
        config, store_path=tmp_path / "live.sqlite3"
    )
    try:
        assert isinstance(service, RoastService)
        assert mcp.running
        # reader + executor are the same adapter instance over the MCP child.
        reader = service._roaster  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert isinstance(reader, RoasterControlAdapter)
        assert service._raw_state is reader  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._exporter is reader  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        # Advisory-paused: no API key → the advisor is None (a regression that
        # wired a non-None advisor without a key would be caught here).
        assert service._advisor is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service.mcp_child_status().value == "running"
    finally:
        await mcp.stop()
        await store.close()


@pytest.mark.asyncio
async def test_build_live_service_fails_closed_on_mcp_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If MCP start raises, the child is stopped and the error propagates — no
    half-wired service is returned (fail-closed invariant)."""
    failing = _FailingProcess()
    monkeypatch.setattr(live, "MCPServerProcess", _failing_process_factory(failing))
    with pytest.raises(MCPConnectionError, match="simulated dead child"):
        await live.build_live_service(AppConfig(), store_path=tmp_path / "live.sqlite3")
    assert failing.stop_called  # the half-started child was cleaned up


@pytest.mark.asyncio
async def test_build_live_service_forwards_coffee_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COFFEE_* forwarding (done before build) populates config.mcp.env, which
    build_server_parameters then passes to the spawned child."""
    config = AppConfig()
    live.forward_coffee_env(config, {"COFFEE_ROASTER_DRIVER": "mock"})
    assert config.mcp.env["COFFEE_ROASTER_DRIVER"] == "mock"

    session = _info_session()
    monkeypatch.setattr(live, "MCPServerProcess", _good_process_factory(session))
    _service, mcp, store = await live.build_live_service(
        config, store_path=tmp_path / "live.sqlite3"
    )
    try:
        params = mcp.build_server_parameters()
        assert params.env is not None
        assert params.env["COFFEE_ROASTER_DRIVER"] == "mock"
    finally:
        await mcp.stop()
        await store.close()


# --- static SPA mount -----------------------------------------------------------


@pytest.fixture
def spa_dir(tmp_path: Path) -> Path:
    """A minimal built-SPA tree: index.html + an asset."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>RoastPilot</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('roastpilot')", encoding="utf-8")
    return root


@pytest_asyncio.fixture
async def served_store(tmp_path: Path) -> AsyncGenerator[RoastStore]:
    instance = RoastStore(tmp_path / "served.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def test_spa_served_at_root(spa_dir: Path) -> None:
    """GET / returns index.html when spa_dir is mounted."""
    app = create_app(spa_dir=spa_dir)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "RoastPilot" in response.text


def test_spa_asset_served(spa_dir: Path) -> None:
    """A real asset path is served from disk, not rewritten to index.html."""
    app = create_app(spa_dir=spa_dir)
    with TestClient(app) as client:
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert "roastpilot" in response.text


def test_spa_client_route_falls_back_to_index(spa_dir: Path) -> None:
    """An unknown non-/api path (a client-side deep link) returns index.html."""
    app = create_app(spa_dir=spa_dir)
    with TestClient(app) as client:
        response = client.get("/history")
        assert response.status_code == 200
        assert "RoastPilot" in response.text


def test_api_health_not_shadowed_by_spa(spa_dir: Path, served_store: RoastStore) -> None:
    """/api/health stays a JSON API response with the SPA mounted at /."""
    app = create_app(RoastService(served_store), spa_dir=spa_dir)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = json.loads(response.text)
        assert body["version"]  # the health payload, not the SPA shell


def test_unknown_api_path_is_json_404_not_spa(spa_dir: Path, served_store: RoastStore) -> None:
    """An unknown /api/* path stays a 404 — the SPA mount never shadows the API
    namespace by rewriting it to index.html."""
    app = create_app(RoastService(served_store), spa_dir=spa_dir)
    with TestClient(app) as client:
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert "RoastPilot" not in response.text


def test_no_spa_mount_without_spa_dir(served_store: RoastStore) -> None:
    """With no spa_dir, nothing is mounted at / (scaffold/API-only shape)."""
    app = create_app(RoastService(served_store))
    with TestClient(app) as client:
        assert client.get("/").status_code == 404  # no SPA route
        assert client.get("/api/health").status_code == 200


def test_no_spa_mount_when_index_missing(tmp_path: Path, served_store: RoastStore) -> None:
    """A spa_dir without index.html mounts nothing (no broken tree served)."""
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    app = create_app(RoastService(served_store), spa_dir=empty)
    with TestClient(app) as client:
        assert client.get("/").status_code == 404


# --- serve smoke (live service + SPA through the real app) ----------------------


@pytest.mark.asyncio
async def test_serve_smoke_live_service_with_spa(
    tmp_path: Path, spa_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end smoke: build the live service with a fake MCP child, mount it
    with the SPA, and serve through the real app — GET / returns the SPA shell
    and GET /api/health works, over the recovery lifespan (create_app's default,
    the same one the serve CLI path passes)."""
    session = _info_session()
    monkeypatch.setattr(live, "MCPServerProcess", _good_process_factory(session))
    service, mcp, store = await live.build_live_service(
        AppConfig(), store_path=tmp_path / "live.sqlite3"
    )
    await store.initialize()
    try:
        # create_app's default lifespan IS the recovery _lifespan (recover_on_start
        # → operator_recovery_required, never an auto-resume); the serve CLI path
        # passes that same lifespan explicitly.
        app = create_app(service, spa_dir=spa_dir)
        # TestClient runs the lifespan (recover_on_start over an empty store is a
        # no-op) so this also exercises the recovery startup path.
        with TestClient(app) as client:
            assert "RoastPilot" in client.get("/").text
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["mcp_child"] == "running"
    finally:
        await mcp.stop()
        await store.close()


# --- restart-recovery invariant (positive) --------------------------------------


def _profile() -> RoastProfile:
    return RoastProfile(
        name="House Espresso",
        bean_origin="Ethiopia",
        bean_varietal="Heirloom",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


@pytest.mark.asyncio
async def test_serve_recovery_resolves_active_run_without_auto_resume(
    tmp_path: Path, spa_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted, non-terminal run is classified into
    ``operator_recovery_required`` on serve startup — positively proving the
    restart-never-auto-resumes invariant (heat/fan are NOT auto-resumed).

    Seeds a run mid-roast (``preheating``) into the live store path, builds the
    live service over a fake MCP child, then runs the app through ``TestClient``
    so the recovery lifespan fires; the run must resolve to
    ``operator_recovery_required``, not stay in/advance from ``preheating``.
    """
    store_path = tmp_path / "recover.sqlite3"

    # Pre-seed a possibly-active run at preheating (no completed_at → recoverable).
    seed_store = RoastStore(store_path)
    await seed_store.initialize()
    await seed_store.create_run(
        run_id="run-recover",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    await seed_store.close()

    # build_live_service must NOT auto-start a background loop while we assert the
    # recovered phase, so disable run_loop on the constructed service.
    session = _info_session()
    monkeypatch.setattr(live, "MCPServerProcess", _good_process_factory(session))
    service, mcp, store = await live.build_live_service(AppConfig(), store_path=store_path)
    # The service was built with the default run_loop=True; pin it off for a
    # deterministic assertion (recover persists the phase synchronously at
    # startup regardless, but this keeps the fake child from being polled).
    service._run_loop = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    await store.initialize()
    try:
        app = create_app(service, spa_dir=spa_dir)
        with TestClient(app) as client:
            # The recovery lifespan ran at startup. The run must be in recovery,
            # never auto-resumed to a hardware-active phase.
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["active_run_id"] == "run-recover"

            detail = client.get("/api/roasts/run-recover")
            assert detail.status_code == 200
            assert detail.json()["agent_phase"] == RoastPhase.OPERATOR_RECOVERY_REQUIRED.value
    finally:
        await service.shutdown()
        await mcp.stop()
        await store.close()


# --- default spa-dir resolution -------------------------------------------------


def test_default_spa_dir_returns_web_dist_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """default_spa_dir resolves the repo's web/dist when it has an index.html.

    No bundled package data exists in this editable/source-checkout test
    environment, so ``_packaged_spa_dir`` is forced to ``None`` to isolate this
    test from whether a prior test in the session happens to have built
    ``web/dist`` under ``_web_dist`` too (it never does today, but this keeps
    the test asserting the source-checkout branch specifically, which is what
    its name promises).
    """
    monkeypatch.setattr(live, "_packaged_spa_dir", lambda: None)
    resolved = live.default_spa_dir()
    # The repo ships a built web/dist; if present it must point at it.
    expected = Path(live.__file__).resolve().parents[2] / "web" / "dist"
    if (expected / "index.html").is_file():
        assert resolved == expected
    else:  # pragma: no cover — only when web/dist is absent in a checkout
        assert resolved is None


def test_default_spa_dir_none_when_build_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no built SPA exists at <pkg>/../../web/dist, default_spa_dir is None.

    Isolated from repo state: point the module's ``__file__`` three levels under
    a tmp dir with no ``web/dist`` so ``parents[2]`` resolves there. Also forces
    ``_packaged_spa_dir`` to ``None`` so this test isolates the source-checkout
    fallback branch specifically.
    """
    fake_pkg = tmp_path / "src" / "roastpilot_agent"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(live, "__file__", str(fake_pkg / "live.py"))
    monkeypatch.setattr(live, "_packaged_spa_dir", lambda: None)
    assert live.default_spa_dir() is None


def test_default_spa_dir_prefers_packaged_data_over_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundled ``_web_dist`` (simulated wheel install) wins over web/dist.

    This is the E11-S1 (#137) precedence: a real wheel install never has a
    sibling ``web/dist`` source checkout, but the order still matters — the
    packaged data must be tried first so a wheel install never accidentally
    picks up a stale/unrelated source-checkout build sitting next to it.
    """
    packaged = Path("/fake/site-packages/roastpilot_agent/_web_dist")
    monkeypatch.setattr(live, "_packaged_spa_dir", lambda: packaged)
    assert live.default_spa_dir() == packaged


def test_packaged_spa_dir_returns_none_when_no_web_dist_package_data() -> None:
    """_packaged_spa_dir is None in this editable install (no _web_dist data).

    The build hook that creates ``roastpilot_agent/_web_dist`` only runs for a
    standard (non-editable) wheel build (see ``hatch_build.py``), so a
    ``pip install -e .`` dev environment — this test environment — never has
    it.
    """
    assert live._packaged_spa_dir() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


def test_packaged_spa_dir_resolves_real_path_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_packaged_spa_dir resolves a real filesystem package-data directory.

    Simulates the pip-installed-as-directory case (what a real wheel install
    always is) by monkeypatching ``importlib.resources.files`` to return a
    ``Path`` under ``tmp_path`` holding an ``index.html``.
    """
    from importlib import resources

    fake_web_dist = tmp_path / "_web_dist"
    fake_web_dist.mkdir()
    (fake_web_dist / "index.html").write_text("<html></html>")
    fake_package_root = tmp_path

    def _fake_files(package: str) -> Path:
        assert package == "roastpilot_agent"
        return fake_package_root

    monkeypatch.setattr(resources, "files", _fake_files)
    assert live._packaged_spa_dir() == fake_web_dist  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


def test_packaged_spa_dir_none_when_web_dist_missing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_packaged_spa_dir is None when the resolved dir has no index.html."""
    from importlib import resources

    (tmp_path / "_web_dist").mkdir()  # no index.html inside

    def _fake_files(package: str) -> Path:
        assert package == "roastpilot_agent"
        return tmp_path

    monkeypatch.setattr(resources, "files", _fake_files)
    assert live._packaged_spa_dir() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


# --- #308 persistence seam: controller dev% wins over the MCP raw dev% ---------


class _SnapshotOnlyController:
    """A minimal controller stand-in exposing just ``snapshot()`` (the only method
    ``RoastRunner._publish_and_persist_telemetry`` calls)."""

    def __init__(self, snapshot: ControllerSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> ControllerSnapshot:
        return self._snapshot

    def set_snapshot(self, snapshot: ControllerSnapshot) -> None:
        """Replace the snapshot returned by this test double."""
        self._snapshot = snapshot


class _RawStateStub:
    """A ``RawStateSource`` whose ``last_state`` carries a chosen MCP raw dev%."""

    def __init__(self, last_state: RoastSessionState) -> None:
        self._last_state = last_state

    @property
    def last_state(self) -> RoastSessionState:
        return self._last_state


@pytest.mark.asyncio
async def test_persisted_dev_percent_is_the_controller_value_not_mcp_raw(
    tmp_path: Path,
) -> None:
    """#308 regression: the PERSISTED telemetry ``development_percent`` is the
    CONTROLLER's charge/FC-referenced value (the single source the advisor reasons
    on), NOT the MCP raw number — which can lag or disagree (the first supervised
    roast persisted the MCP's value while the operator-facing dev% must match the
    model's). Locks the api.py persistence-swap so it cannot be silently reverted.

    The MCP raw state reports development_percent = 3.6 (the canned fixture); the
    controller snapshot reports a deliberately DIFFERENT 12.5. The stored row must
    carry 12.5.
    """
    store = RoastStore(tmp_path / "seam.sqlite3")
    await store.initialize()
    try:
        run_id = "run-seam"
        await store.create_run(
            run_id=run_id,
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.DEVELOPMENT,
        )
        controller_dev_percent = 12.5
        mcp_raw = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
        assert mcp_raw.development_percent == 3.6  # the differing MCP raw value
        snapshot = ControllerSnapshot(
            phase=RoastPhase.DEVELOPMENT,
            current_heat=50,
            current_fan=60,
            roast_elapsed_seconds=600.0,
            charge_elapsed_seconds=480.0,
            development_elapsed_seconds=75.0,
            development_percent=controller_dev_percent,
            post_fc_recovery_enabled=True,
            post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            post_fc_ror_setpoint_c_per_min=6.4,
            post_fc_smoothed_ror_c_per_min=4.8,
            post_fc_effective_heat_ceiling_percent=75,
            telemetry=RoastTelemetry.model_validate({"bean_temp_c": 196.0, "env_temp_c": 214.0}),
            advisory_paused=False,
            charge_detected=True,
        )
        queue: asyncio.Queue[QueuedOperatorAction] = asyncio.Queue()
        runner = RoastRunner(
            # Duck-typed: the persistence method calls only ``snapshot()``.
            controller=cast(RoastController, _SnapshotOnlyController(snapshot)),
            store=store,
            emitter=BufferingEventEmitter(EventBroadcaster(), clock=lambda: 0.0),
            operator_queue=queue,
            counter=_TickCounter(),
            config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
            run_id=run_id,
            clock=lambda: 0.0,
            raw_state=_RawStateStub(mcp_raw),
        )

        await runner._publish_and_persist_telemetry()  # pyright: ignore[reportPrivateUsage]

        points = await store.read_telemetry_points(run_id)
        assert len(points) == 1
        assert points[0].development_percent == controller_dev_percent  # controller, not 3.6
        # #308: the charge-referenced roast clock is persisted from the snapshot
        # (the REST telemetry series re-origins the chart x-axis at charge).
        assert points[0].charge_elapsed_seconds == 480.0
        assert points[0].post_fc_recovery_enabled is True
        assert points[0].post_fc_heat_authority_state is PostFcHeatAuthorityState.RECOVERING
        assert points[0].post_fc_ror_setpoint_c_per_min == pytest.approx(6.4)
        assert points[0].post_fc_smoothed_ror_c_per_min == pytest.approx(4.8)
        assert points[0].post_fc_effective_heat_ceiling_percent == 75
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runner_persists_same_tick_accepted_trace_but_emits_current_authority(
    tmp_path: Path,
) -> None:
    """Historical recovery survives a same-tick drop without stale live SSE."""
    store = RoastStore(tmp_path / "accepted-trace.sqlite3")
    await store.initialize()
    try:
        run_id = "run-accepted-trace"
        await store.create_run(
            run_id=run_id,
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.COOLING,
        )
        accepted = PostFcControlOutput(
            heat_percent=66,
            setpoint_c_per_min=6.4,
            error_c_per_min=1.6,
            smoothed_ror_c_per_min=4.8,
            integrator=8.0,
            effective_ceiling_percent=75,
            effective_floor_percent=1,
            saturated=False,
            heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            recovery_active=True,
        )
        snapshot = ControllerSnapshot(
            phase=RoastPhase.COOLING,
            current_heat=0,
            current_fan=100,
            roast_elapsed_seconds=600.0,
            charge_elapsed_seconds=480.0,
            development_elapsed_seconds=75.0,
            development_percent=15.625,
            post_fc_recovery_enabled=True,
            post_fc_heat_authority_state=None,
            post_fc_ror_setpoint_c_per_min=None,
            post_fc_smoothed_ror_c_per_min=None,
            post_fc_effective_heat_ceiling_percent=None,
            telemetry=RoastTelemetry.model_validate(
                {"bean_temp_c": 196.0, "env_temp_c": 214.0, "cooling_on": True}
            ),
            advisory_paused=False,
            charge_detected=True,
            accepted_post_fc_output=accepted,
        )
        controller = _SnapshotOnlyController(snapshot)
        broadcaster = EventBroadcaster()
        subscriber = broadcaster.subscribe()
        runner = RoastRunner(
            controller=cast(RoastController, controller),
            store=store,
            emitter=BufferingEventEmitter(broadcaster, clock=lambda: 0.0),
            operator_queue=asyncio.Queue[QueuedOperatorAction](),
            counter=_TickCounter(),
            config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=5.0)),
            run_id=run_id,
            clock=lambda: 0.0,
            raw_state=None,
        )

        await runner._publish_and_persist_telemetry()  # pyright: ignore[reportPrivateUsage]
        live = subscriber.get_nowait()
        assert live.event.value == "telemetry"
        assert live.data["agent_phase"] == "cooling"
        assert live.data["post_fc_heat_authority_state"] is None
        assert live.data["post_fc_ror_setpoint_c_per_min"] is None

        points = await store.read_telemetry_points(run_id)
        assert len(points) == 1
        assert points[0].agent_phase is RoastPhase.COOLING
        assert points[0].post_fc_heat_authority_state is PostFcHeatAuthorityState.RECOVERING
        assert points[0].post_fc_ror_setpoint_c_per_min == pytest.approx(6.4)

        # The next cooling tick resets the tick-scoped witness. Its null state
        # bypasses the 5 s periodic throttle because it closes the D96 trace.
        controller.set_snapshot(
            replace(
                snapshot,
                roast_elapsed_seconds=600.5,
                accepted_post_fc_output=None,
            )
        )
        await runner._publish_and_persist_telemetry()  # pyright: ignore[reportPrivateUsage]
        next_live = subscriber.get_nowait()
        assert next_live.data["post_fc_heat_authority_state"] is None
        points = await store.read_telemetry_points(run_id)
        assert [point.post_fc_heat_authority_state for point in points] == [
            PostFcHeatAuthorityState.RECOVERING,
            None,
        ]
    finally:
        await store.close()
