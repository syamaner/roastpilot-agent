"""Live-serve composition tests (E11-S1 early item; supervised roast #134).

Hardware-free: ``build_live_service`` is exercised with a fake stdio session
injected into the real :class:`MCPServerProcess` via its ``session_factory``
seam (the same seam ``test_mcp_client.py`` uses), so no ``coffee-roaster-mcp``
binary, hardware, or network is touched. The static-mount tests use FastAPI's
synchronous ``TestClient`` over a temporary built-SPA tree.
"""

import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from roastpilot_agent import live
from roastpilot_agent.api import RoastService, create_app
from roastpilot_agent.config import AdvisorConfig, AppConfig, MCPConfig
from roastpilot_agent.mcp_client import (
    MCPConnectionError,
    MCPServerProcess,
    RoasterControlAdapter,
)
from roastpilot_agent.store import RoastStore

# Reuse the canned tool fixtures + fake-session doubles from the mcp_client tests.
from tests.test_mcp_client import (
    CANNED,
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
) -> Callable[[MCPConfig], MCPServerProcess]:
    """A drop-in for ``live.MCPServerProcess`` that injects a healthy fake
    session, so ``build_live_service`` starts/health-checks with no hardware."""

    def _factory(config: MCPConfig) -> MCPServerProcess:
        return MCPServerProcess(config, session_factory=_GoodFactory(session))

    return _factory


class _FailingProcess(MCPServerProcess):
    """An MCPServerProcess whose start() always fails, tracking stop() calls."""

    def __init__(self, config: MCPConfig | None = None) -> None:
        super().__init__(config)
        self.stop_called = False

    async def start(self) -> None:
        raise MCPConnectionError("simulated dead child")

    async def stop(self) -> None:
        self.stop_called = True
        await super().stop()


def _failing_process_factory(
    process: _FailingProcess,
) -> Callable[[MCPConfig], MCPServerProcess]:
    """A drop-in for ``live.MCPServerProcess`` returning the failing double."""

    def _factory(config: MCPConfig) -> MCPServerProcess:
        return process

    return _factory


@pytest.mark.asyncio
async def test_build_live_service_wires_adapter_as_reader_and_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's roaster surface is the RoasterControlAdapter over the MCP
    child (reader + executor are the same adapter, the controller never sees the
    raw client)."""
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


# --- default spa-dir resolution -------------------------------------------------


def test_default_spa_dir_returns_web_dist_when_present() -> None:
    """default_spa_dir resolves the repo's web/dist when it has an index.html."""
    resolved = live.default_spa_dir()
    # The repo ships a built web/dist; if present it must point at it.
    expected = Path(live.__file__).resolve().parents[2] / "web" / "dist"
    if (expected / "index.html").is_file():
        assert resolved == expected
    else:  # pragma: no cover — only when web/dist is absent in a checkout
        assert resolved is None
