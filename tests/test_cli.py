"""CLI tests (E10-S1): argument parsing + the ``--replay`` serve dispatch.

Hardware-free and server-free: the serve path is exercised with uvicorn's
``Server.serve`` patched to a no-op, so the replay app is built and the run is
driven without binding a socket.
"""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from roastpilot_agent import cli


@pytest.fixture(autouse=True)
def isolate_live_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a serve test write the live DB into the real ``~/.local/state``.

    ``_serve_live`` resolves a PERSISTENT store path (#161) and creates its
    parent directory, so by default any serve test would ``mkdir`` under the
    real home. Pin the default at a per-test tmp via ``XDG_STATE_HOME`` and
    clear any ambient ``ROASTPILOT_DB``; tests that assert precedence override
    these themselves (a later ``monkeypatch.setenv`` wins).
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("ROASTPILOT_DB", raising=False)


def _runtime_config_payload(
    *, roaster_driver: str = "hottop_kn8828b_2k_plus", first_crack_mode: str = "audio"
) -> dict[str, Any]:
    """A full ``get_runtime_config`` raw payload for a fake MCP ``call_tool``.

    Carries every field ``RuntimeConfigSnapshot`` requires so a fake child can
    answer the startup readout's single read-only ``get_runtime_config`` call.
    """
    return {
        "config_source": "/tmp/coffee.yaml",
        "roaster_driver": roaster_driver,
        "roaster_port": "/dev/cu.usbserial-XYZ",
        "roaster_baudrate": 115200,
        "temperature_unit": "celsius",
        "command_interval_seconds": 1.0,
        "first_crack_mode": first_crack_mode,
        "model_repo_id": "syamaner/coffee-first-crack-detection",
        "model_precision": "int8",
        "allow_manual_override": False,
        "log_dir": "/tmp/roast-logs",
        "sample_interval_seconds": 1.0,
        "auto_t0_detection_enabled": True,
        "auto_t0_drop_threshold_c": 5.0,
    }


def _make_call_tool(
    payload: dict[str, Any] | None = None,
) -> Callable[[str, dict[str, object]], Any]:
    """A fake MCP ``call_tool`` that answers ``get_runtime_config`` from a payload."""

    async def _call_tool(name: str, arguments: dict[str, object]) -> object:
        if name == "get_runtime_config":
            return payload if payload is not None else _runtime_config_payload()
        raise AssertionError(f"unexpected tool call {name!r} during readout")

    return _call_tool


def test_version_flag_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--version`` prints the version and exits 0 (argparse action)."""
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "roastpilot-agent" in capsys.readouterr().out


def test_no_args_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no ``--replay``, main prints help and returns 0 (scaffold mode)."""
    monkeypatch.setattr("sys.argv", ["roastpilot-agent"])
    assert cli.main() == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_replay_missing_jsonl_returns_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay`` on a directory with no roast.jsonl returns exit code 2."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "--replay", str(empty)])
    assert cli.main() == 2
    assert "no roast.jsonl" in capsys.readouterr().out


@pytest.fixture
def no_serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Patch uvicorn's Server.serve to a no-op so no socket is bound."""
    import uvicorn

    async def _fake_serve(self: Any) -> None:  # noqa: ANN401 — patched method
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_serve)
    yield


@pytest.mark.usefixtures("no_serve")
def test_replay_step_mode_builds_and_serves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay <dir> --step`` builds the app, prints the run banner, and
    returns 0 — paused (no frames advanced) since stepping is HTTP-driven."""
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "replaying session-2" in out
    assert "stepped (paused at tick 0)" in out


@pytest.mark.usefixtures("no_serve")
def test_replay_free_running_drives_the_roast(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay <dir> --speed 60`` (no --step) free-runs the recorded roast to
    completion via run() — the banner reports the free-running mode and notes the
    serve-final-frame-after-completion behaviour (#103). The fault fixture is used
    (9 frames) so the inter-tick sleeps stay negligible."""
    fixture = Path(__file__).parent / "fixtures" / "replay" / "fault-pre-t0"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--speed", "60", "--port", "0"],
    )
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "free-running at 60x" in out
    # #103: the free-running banner flags the non-obvious "keeps serving after the
    # roast ends" behaviour so an operator does not think the rig has hung.
    assert "serves the final frame after the roast ends" in out


def test_parser_serve_defaults() -> None:
    """``serve`` parses with the default host/port/spa-dir."""
    args = cli._build_parser().parse_args(["serve"])  # pyright: ignore[reportPrivateUsage]
    assert args.action == "serve"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.spa_dir is None


def test_parser_serve_with_spa_dir(tmp_path: Path) -> None:
    """``serve --spa-dir`` parses the override path."""
    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    args = parser.parse_args(["serve", "--spa-dir", str(tmp_path)])
    assert args.action == "serve"
    assert args.spa_dir == tmp_path


def test_parser_access_log_defaults_to_none() -> None:
    """``--access-log`` / ``--log-level`` default to ``None`` (config wins)."""
    args = cli._build_parser().parse_args(["serve"])  # pyright: ignore[reportPrivateUsage]
    assert args.access_log is None
    assert args.log_level is None


def test_parser_access_log_accepts_modes() -> None:
    """``--access-log`` accepts the three modes; ``--log-level`` is free text."""
    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    for mode in ("quiet", "full", "off"):
        args = parser.parse_args(["serve", "--access-log", mode, "--log-level", "debug"])
        assert args.access_log == mode
        assert args.log_level == "debug"


def test_parser_access_log_rejects_bad_mode() -> None:
    """``--access-log`` rejects an out-of-set mode (argparse ``choices``)."""
    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--access-log", "loud"])


def test_resolve_spa_dir_prefers_explicit_when_valid(tmp_path: Path) -> None:
    """An explicit --spa-dir with index.html is used verbatim."""
    (tmp_path / "index.html").write_text("<title>x</title>", encoding="utf-8")
    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    args = parser.parse_args(["serve", "--spa-dir", str(tmp_path)])
    assert cli._resolve_spa_dir(args) == tmp_path  # pyright: ignore[reportPrivateUsage]


def test_resolve_spa_dir_none_when_explicit_invalid(tmp_path: Path) -> None:
    """An explicit --spa-dir without index.html resolves to None (mount nothing)."""
    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    args = parser.parse_args(["serve", "--spa-dir", str(tmp_path)])
    assert cli._resolve_spa_dir(args) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("no_serve")
def test_serve_fails_closed_on_mcp_start_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``serve`` returns a non-zero exit with a clear message when the MCP child
    cannot start — fail-closed, no socket bound."""
    from roastpilot_agent import live
    from roastpilot_agent.mcp_client import MCPConnectionError

    async def _boom(config: object, *, store_path: object) -> object:  # noqa: ANN401
        raise MCPConnectionError("no coffee-roaster-mcp on PATH")

    monkeypatch.setattr(live, "build_live_service", _boom)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])
    assert cli.main() == 1
    assert "could not start coffee-roaster-mcp" in capsys.readouterr().out


@pytest.mark.usefixtures("no_serve")
def test_serve_live_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``serve`` builds the live app over a fake MCP child, prints the banner,
    serves (no-op), and runs the ``finally`` teardown in order: service.shutdown
    → mcp.stop → store.close.

    Exercises the whole ``_serve_live`` happy path + teardown without a real MCP,
    a socket, or a network call. Also covers COFFEE_* forwarding through the CLI
    (it is forwarded into config.mcp.env before build_live_service runs) and the
    SPA-dir resolution (an explicit --spa-dir with index.html is mounted).
    """
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    # Operator-style env: COFFEE_* must reach config.mcp.env via forward_coffee_env.
    monkeypatch.setenv("COFFEE_ROASTER_DRIVER", "mock")

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text("<title>RoastPilot</title>", encoding="utf-8")

    order: list[str] = []
    captured: dict[str, object] = {}

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool(_runtime_config_payload(roaster_driver="mock")))

        async def stop(self) -> None:
            order.append("mcp.stop")

    class _RecordingStore(RoastStore):
        async def close(self) -> None:
            order.append("store.close")
            await super().close()

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        # Assert the CLI forwarded COFFEE_* into the config before building.
        captured["coffee_driver"] = config.mcp.env.get("COFFEE_ROASTER_DRIVER")
        captured["store_path"] = store_path
        store = _RecordingStore(store_path)
        service = RoastService(store)
        original_shutdown = service.shutdown

        async def _tracked_shutdown() -> None:
            order.append("service.shutdown")
            await original_shutdown()

        monkeypatch.setattr(service, "shutdown", _tracked_shutdown)
        return service, _FakeMCP(), store

    live_db = tmp_path / "trace" / "live.sqlite3"
    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "serve", "--port", "0", "--spa-dir", str(spa), "--db", str(live_db)],
    )

    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "serving live roast (with SPA)" in out
    # #161: the live store is the PERSISTENT --db path (never a tempdir), its
    # parent dir was created, and the path is printed in the operator readout.
    assert captured["store_path"] == live_db
    assert live_db.parent.is_dir()
    assert f"decision trace → {live_db}" in out
    # The startup runtime readout printed (driver/port/FC-mode/model) before serve.
    assert "Roaster runtime (from coffee-roaster-mcp)" in out
    assert "/dev/cu.usbserial-XYZ" in out
    assert "syamaner/coffee-first-crack-detection" in out
    # The mock-driver warning fired (this fake child resolved the mock driver).
    assert "MOCK driver — NOT real hardware" in out
    # The advisor reachability readout printed before serve (#168). This fake
    # service has no advisor, so the probe reports NOT CONFIGURED — a regression
    # that silently dropped the readout call would fail here.
    assert "advisor NOT CONFIGURED" in out
    # COFFEE_* was forwarded through the CLI into config.mcp.env.
    assert captured["coffee_driver"] == "mock"
    # The finally teardown ran in order: service.shutdown → mcp.stop → store.close.
    assert order == ["service.shutdown", "mcp.stop", "store.close"]


@pytest.mark.usefixtures("no_serve")
def test_serve_live_path_installs_recovery_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live ``serve`` path wires the recovery lifespan (issue #104).

    Architecture invariant: a restart over a possibly-active run must enter
    ``operator_recovery_required`` and NEVER auto-resume heat/fan. ``_serve_live``
    builds the app with ``create_app(service, spa_dir=...)`` and deliberately
    passes NO ``lifespan`` override, so it gets the default recovery ``_lifespan``
    (``recover_on_start``) — replay's no-recovery lifespan must never reach this
    path.

    This pins the *observable guarantee* to the actual live serve path, not a
    symbol reference: it pre-seeds a mid-roast (``preheating``) run into the live
    store, drives the real ``cli.main() → _serve_live`` over a fake MCP child
    (uvicorn ``serve`` is a no-op, so the app is built but its lifespan is not
    yet entered), captures the app object ``_serve_live`` actually constructs by
    wrapping ``create_app`` (the real factory still runs, default lifespan
    intact), and then enters that captured app's lifespan. The recovery lifespan
    must classify the run into ``operator_recovery_required`` with the roaster
    untouched. If anyone swaps ``_serve_live`` to a bare/no-recovery lifespan,
    the run would stay ``preheating`` and this test fails.
    """
    import roastpilot_agent.api as api
    from roastpilot_agent import live
    from roastpilot_agent.advisor import FakeAdvisor
    from roastpilot_agent.api import RoastService, create_app
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.models import RoastPhase, RoastProfile, RoastTelemetry
    from roastpilot_agent.store import RoastStore
    from tests.conftest import FakeMCPClient

    store_path = tmp_path / "trace" / "live.sqlite3"
    profile = RoastProfile(
        name="House Espresso",
        bean_origin="Ethiopia",
        bean_varietal="Heirloom",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool(_runtime_config_payload(roaster_driver="mock")))

        async def stop(self) -> None:
            return None

    async def _seed_then_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        # Pre-seed a possibly-active run mid-roast (no completed_at → recoverable)
        # in the SAME store the wired service reads, then return a genuinely-wired
        # service (real roaster + advisor) so recover_on_start actually classifies.
        store = RoastStore(store_path)
        await store.initialize()
        await store.create_run(
            run_id="run-104",
            profile=profile,
            config=config,
            agent_phase=RoastPhase.PREHEATING,
        )
        roaster = FakeMCPClient([RoastTelemetry(bean_temp_c=150.0, env_temp_c=160.0)])
        service = RoastService(
            store,
            config=config,
            mcp=_FakeMCP(),  # type: ignore[arg-type]  # liveness-only handle
            roaster=roaster,
            advisor=FakeAdvisor(),
            run_loop=False,  # don't auto-start a loop; assert the recovered phase
        )
        return service, _FakeMCP(), store

    # Wrap create_app so we capture the app _serve_live actually builds, while the
    # REAL factory (with its default recovery lifespan) runs unchanged.
    built_apps: list[Any] = []
    real_create_app = create_app

    def _capturing_create_app(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        app = real_create_app(*args, **kwargs)
        # A no-recovery lifespan would be an explicit kwarg — assert the live path
        # never passes one, so it cannot opt out of recovery.
        assert "lifespan" not in kwargs, "live serve path must not override the recovery lifespan"
        built_apps.append(app)
        return app

    monkeypatch.setattr(api, "create_app", _capturing_create_app)
    monkeypatch.setattr(live, "build_live_service", _seed_then_build)
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "serve", "--port", "0", "--db", str(store_path)],
    )

    assert cli.main() == 0
    assert len(built_apps) == 1, "_serve_live should build exactly one app"
    app = built_apps[0]

    async def _assert_recovery() -> None:
        # _serve_live's teardown closed the store; re-open it (initialize is
        # idempotent and re-connects to the same on-disk DB, where the seeded
        # mid-roast run still lives) before entering the lifespan.
        store: RoastStore = app.state.service._store  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await store.initialize()
        try:
            # Enter the captured app's lifespan — the one _serve_live built. The
            # recovery _lifespan runs recover_on_start; the run must land in
            # recovery, never auto-resumed to a hardware-active phase, with no
            # roaster write.
            async with app.router.lifespan_context(app):
                recovered = await store.read_run("run-104")
                assert recovered is not None
                assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
                roaster = app.state.service._roaster  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                assert roaster.calls == [], (
                    "recovery must not issue any roaster write (no auto-resume)"
                )
        finally:
            await store.close()

    asyncio.run(_assert_recovery())


def test_resolve_live_store_path_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--db`` > ``ROASTPILOT_DB`` > ``$XDG_STATE_HOME`` default, and the
    parent directory is always created (issue #161)."""
    import argparse

    # 1. Explicit --db wins over env and default; parent is created.
    flag_db = tmp_path / "flag" / "explicit.sqlite3"
    monkeypatch.setenv("ROASTPILOT_DB", str(tmp_path / "env" / "from-env.sqlite3"))
    resolved = cli._resolve_live_store_path(argparse.Namespace(db=flag_db))  # pyright: ignore[reportPrivateUsage]
    assert resolved == flag_db
    assert flag_db.parent.is_dir()

    # 2. ROASTPILOT_DB wins over the XDG default when no --db.
    env_db = tmp_path / "env" / "from-env.sqlite3"
    monkeypatch.setenv("ROASTPILOT_DB", str(env_db))
    resolved = cli._resolve_live_store_path(argparse.Namespace(db=None))  # pyright: ignore[reportPrivateUsage]
    assert resolved == env_db
    assert env_db.parent.is_dir()

    # 3. Default is $XDG_STATE_HOME/roastpilot/roastpilot.sqlite3.
    monkeypatch.delenv("ROASTPILOT_DB", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    resolved = cli._resolve_live_store_path(argparse.Namespace(db=None))  # pyright: ignore[reportPrivateUsage]
    assert resolved == tmp_path / "xdg" / "roastpilot" / "roastpilot.sqlite3"
    assert resolved.parent.is_dir()


def test_resolve_live_store_path_home_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``--db``/``ROASTPILOT_DB``/``XDG_STATE_HOME``, the default is
    ``~/.local/state/roastpilot/roastpilot.sqlite3`` (issue #161)."""
    import argparse

    monkeypatch.delenv("ROASTPILOT_DB", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    resolved = cli._resolve_live_store_path(argparse.Namespace(db=None))  # pyright: ignore[reportPrivateUsage]
    assert resolved == tmp_path / "home" / ".local" / "state" / "roastpilot" / "roastpilot.sqlite3"
    assert resolved.parent.is_dir()


def test_db_with_replay_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--db`` is live-serve only; combining it with ``--replay`` exits 2 with a
    clear message rather than silently ignoring the flag (issue #161)."""
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", "somedir", "--db", "/tmp/x.sqlite3"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "--db is only valid for 'serve'" in capsys.readouterr().err


@pytest.mark.usefixtures("no_serve")
def test_serve_no_child_leak_when_post_build_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a post-build step (store.initialize) raises after build_live_service
    returns a RUNNING child, the child is still stopped (no orphan) and the
    error propagates — the whole post-build phase is inside the try/finally."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    stopped: list[str] = []

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool())

        async def stop(self) -> None:
            stopped.append("mcp.stop")

    class _ExplodingStore(RoastStore):
        async def initialize(self) -> None:
            raise RuntimeError("aiosqlite boom on initialize")

        async def close(self) -> None:
            stopped.append("store.close")

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        store = _ExplodingStore(store_path)
        return RoastService(store), _FakeMCP(), store

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])

    with pytest.raises(RuntimeError, match="aiosqlite boom on initialize"):
        cli.main()
    # The child started by build_live_service was torn down, not orphaned.
    assert "mcp.stop" in stopped


@pytest.mark.usefixtures("no_serve")
def test_serve_teardown_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing teardown step (mcp.stop) is logged, never raised, and does not
    abort the rest of the cleanup chain (store.close still runs)."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    ran: list[str] = []

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool())

        async def stop(self) -> None:
            ran.append("mcp.stop")
            raise RuntimeError("stop failed")

    class _RecordingStore(RoastStore):
        async def close(self) -> None:
            ran.append("store.close")
            await super().close()

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        store = _RecordingStore(store_path)
        return RoastService(store), _FakeMCP(), store

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])

    with caplog.at_level("WARNING"):
        assert cli.main() == 0
    # The failing mcp.stop was logged, not raised; store.close still ran after it.
    assert "mcp.stop" in caplog.text
    assert ran == ["mcp.stop", "store.close"]


# --- startup runtime readout (#134) ---------------------------------------------


@pytest.mark.asyncio
async def test_emit_runtime_readout_prints_fields_for_real_hardware(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real-Hottop runtime config prints driver/port/FC-mode/model with no
    warnings (driver != mock, FC mode == audio)."""
    await cli._emit_runtime_readout(_make_call_tool())  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert "Roaster runtime (from coffee-roaster-mcp)" in out
    assert "hottop_kn8828b_2k_plus" in out
    assert "/dev/cu.usbserial-XYZ" in out
    assert "115200" in out
    assert "first crack   : audio" in out
    assert "syamaner/coffee-first-crack-detection · int8" in out
    assert "log dir       : /tmp/roast-logs" in out
    # Real hardware + audio FC: no warnings fire.
    assert "MOCK driver" not in out
    assert "not audio" not in out
    # The mic/FC-listening pointer is always present (not in RuntimeConfigSnapshot).
    assert "mic + FC-listening: confirm on the dashboard" in out


@pytest.mark.asyncio
async def test_emit_runtime_readout_warns_on_mock_driver(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loud mock-driver warning fires when roaster_driver == 'mock'."""
    await cli._emit_runtime_readout(  # pyright: ignore[reportPrivateUsage]
        _make_call_tool(_runtime_config_payload(roaster_driver="mock"))
    )
    out = capsys.readouterr().out
    assert "⚠️" in out
    assert "MOCK driver — NOT real hardware" in out


@pytest.mark.asyncio
async def test_emit_runtime_readout_warns_when_fc_mode_not_audio(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loud FC-not-audio warning fires when first_crack_mode != 'audio'."""
    await cli._emit_runtime_readout(  # pyright: ignore[reportPrivateUsage]
        _make_call_tool(_runtime_config_payload(first_crack_mode="disabled"))
    )
    out = capsys.readouterr().out
    assert "⚠️" in out
    assert "first-crack mode is 'disabled', not audio" in out


@pytest.mark.asyncio
async def test_emit_runtime_readout_logs_and_continues_on_failure(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A get_runtime_config transport failure is logged and swallowed — the
    readout is informational and never a startup blocker (no raise, no readout)."""
    from roastpilot_agent.mcp_client import MCPConnectionError

    async def _boom(name: str, arguments: dict[str, object]) -> object:
        raise MCPConnectionError("MCP call 'get_runtime_config' failed: timeout")

    with caplog.at_level("WARNING"):
        await cli._emit_runtime_readout(_boom)  # pyright: ignore[reportPrivateUsage]  # must not raise
    assert "could not read runtime config" in caplog.text
    # No readout block printed when the read failed.
    assert "Roaster runtime" not in capsys.readouterr().out


# --- startup advisor reachability readout (issue #168) --------------------------


def _advisor_health(status: str, **kw: object) -> Any:
    from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus

    return AdvisorHealth(status=AdvisorHealthStatus(status), **kw)  # type: ignore[arg-type]


def test_format_advisor_readout_reachable() -> None:
    """A REACHABLE probe prints provider + model, no warning glyph."""
    lines = cli._format_advisor_readout(  # pyright: ignore[reportPrivateUsage]
        _advisor_health("reachable", provider="anthropic", model_slug="claude-opus-4.8")
    )
    text = "\n".join(lines)
    assert "advisor REACHABLE" in text
    assert "provider=anthropic" in text
    assert "model=claude-opus-4.8" in text
    assert "⚠️" not in text


def test_format_advisor_readout_unreachable_carries_error() -> None:
    """An UNREACHABLE probe prints a loud ⚠️ line with the actual provider error
    and notes the roast can still start advisory-paused."""
    lines = cli._format_advisor_readout(  # pyright: ignore[reportPrivateUsage]
        _advisor_health(
            "unreachable",
            provider="openai_compatible",
            model_slug="anthropic/claude-opus-4.8",
            error="401 Unauthorized (invalid api key)",
        )
    )
    text = "\n".join(lines)
    assert "⚠️" in text
    assert "advisor UNREACHABLE" in text
    assert "401 Unauthorized" in text
    assert "can still start" in text


def test_format_advisor_readout_not_configured() -> None:
    """A NOT_CONFIGURED probe notes the advisory-paused mode (no key)."""
    lines = cli._format_advisor_readout(  # pyright: ignore[reportPrivateUsage]
        _advisor_health("not_configured")
    )
    text = "\n".join(lines)
    assert "advisor NOT CONFIGURED" in text
    assert "advisory-paused" in text


@pytest.mark.asyncio
async def test_emit_advisor_readout_records_health_and_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The readout probes the service's advisor, prints REACHABLE, and records
    the result on the service so /api/health can surface it."""
    from roastpilot_agent.advisor import FakeAdvisor
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.models import AdvisorHealthStatus
    from roastpilot_agent.store import RoastStore

    store = RoastStore(tmp_path / "t.sqlite3")
    await store.initialize()
    try:
        service = RoastService(store, advisor=FakeAdvisor())
        health = await cli._emit_advisor_readout(service)  # pyright: ignore[reportPrivateUsage]
        assert health.status is AdvisorHealthStatus.REACHABLE
        assert "advisor REACHABLE" in capsys.readouterr().out
        # The probe result is recorded on the service for /api/health.
        served = await service.health()
        assert served.advisor is not None
        assert served.advisor.status is AdvisorHealthStatus.REACHABLE
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_emit_advisor_readout_unreachable_does_not_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An advisor whose probe fails surfaces UNREACHABLE loudly but the readout
    still returns normally — serve is never blocked (advisory-paused is valid)."""
    from roastpilot_agent.advisor import AdvisorProviderError, FakeAdvisor
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.models import AdvisorHealthStatus
    from roastpilot_agent.store import RoastStore

    advisor = FakeAdvisor(health=AdvisorProviderError("402 Payment Required"))
    service = RoastService(RoastStore(tmp_path / "t.sqlite3"), advisor=advisor)
    health = await cli._emit_advisor_readout(service)  # pyright: ignore[reportPrivateUsage]
    assert health.status is AdvisorHealthStatus.UNREACHABLE
    out = capsys.readouterr().out
    assert "⚠️" in out
    assert "402 Payment Required" in out
