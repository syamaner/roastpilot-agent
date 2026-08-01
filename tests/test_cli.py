"""CLI tests (E10-S1): argument parsing + the ``--replay`` serve dispatch.

Hardware-free and server-free: the serve path is exercised with uvicorn's
``Server.serve`` patched to a no-op, so the replay app is built and the run is
driven without binding a socket.
"""

import asyncio
import os
import signal
import threading
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from roastpilot_agent import cli


class _ForcedProcessExit(BaseException):
    """Test sentinel replacing ``os._exit`` in guard unit tests."""

    def __init__(self, code: int) -> None:
        """Record the requested process exit code."""
        self.code = code


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


def test_live_exit_guard_reports_survivors_before_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog expiry reports safe task labels and requests exit 70."""
    guard = object.__new__(cli._LiveExitGuard)  # pyright: ignore[reportPrivateUsage]
    guard._grace_seconds = 0.0  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._armed = threading.Event()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._armed.set()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._disarmed = threading.Event()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._residual_labels = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        "retained-mcp-owner",
    )
    written: list[bytes] = []

    def _capture_write(_fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    monkeypatch.setattr(os, "write", _capture_write)

    def _forced_exit(code: int) -> None:
        raise _ForcedProcessExit(code)

    monkeypatch.setattr(os, "_exit", _forced_exit)
    with pytest.raises(_ForcedProcessExit) as exc:
        guard._watch()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code == 70
    assert b"retained-mcp-owner" in b"".join(written)


def test_live_signal_guard_delegates_first_and_forces_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First SIGINT is graceful; a second reports uncertainty and exits 70."""
    graceful: list[int] = []
    written: list[bytes] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    def _capture_write(_fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    monkeypatch.setattr(os, "write", _capture_write)

    def _forced_exit(code: int) -> None:
        raise _ForcedProcessExit(code)

    monkeypatch.setattr(os, "_exit", _forced_exit)
    guard._handle(2, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    assert guard.received_signal == signal.SIGINT
    assert graceful == [2]
    with pytest.raises(_ForcedProcessExit) as exc:
        guard._handle(2, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code == 70
    assert b"hardware state is uncertain" in b"".join(written)


def test_live_signal_guard_delegates_sigterm_to_graceful_shutdown() -> None:
    """SIGTERM retains Uvicorn's graceful path instead of bypassing teardown."""
    graceful: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    guard._handle(signal.SIGTERM, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert guard.received_signal == signal.SIGTERM
    assert graceful == [signal.SIGTERM]


def test_live_signal_guard_repeated_sigterm_remains_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated SIGTERM never takes SIGINT's immediate-force escape path."""
    graceful: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))
    forced: list[int] = []
    monkeypatch.setattr(os, "_exit", forced.append)

    guard._handle(signal.SIGTERM, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._handle(signal.SIGTERM, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert graceful == [signal.SIGTERM, signal.SIGTERM]
    assert forced == []


@pytest.mark.parametrize(
    ("first", "second", "expected_graceful"),
    [
        (signal.SIGINT, signal.SIGTERM, [signal.SIGINT, signal.SIGTERM]),
        (signal.SIGTERM, signal.SIGINT, [signal.SIGTERM]),
    ],
)
def test_live_signal_guard_preserves_first_signal_for_exit_semantics(
    first: int,
    second: int,
    expected_graceful: list[int],
) -> None:
    """Mixed graceful signals retain the first signal's process outcome."""
    graceful: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    guard._handle(first, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._handle(second, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert guard.received_signal == first
    assert graceful == expected_graceful


def test_sigterm_then_sigint_does_not_force_uvicorn_exit() -> None:
    """A first SIGINT after SIGTERM cannot skip application shutdown."""
    server = cli._SignalManagedServer(uvicorn.Config("tests.test_cli:app"))  # pyright: ignore[reportPrivateUsage]
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(server.handle_exit)

    guard._handle(signal.SIGTERM, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._handle(signal.SIGINT, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert server.should_exit is True
    assert server.force_exit is False
    assert guard.received_signal == signal.SIGTERM


def test_sigterm_then_two_sigints_still_forces_explicit_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the second actual SIGINT forces exit, even after SIGTERM."""
    graceful: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    def _forced_exit(code: int) -> None:
        raise _ForcedProcessExit(code)

    monkeypatch.setattr(os, "_exit", _forced_exit)
    guard._handle(signal.SIGTERM, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    guard._handle(signal.SIGINT, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(_ForcedProcessExit) as exc:
        guard._handle(signal.SIGINT, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert exc.value.code == 70
    assert graceful == [signal.SIGTERM]


def test_live_signal_guard_handles_platform_sigbreak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform SIGBREAK is installed and remains a graceful signal."""
    sigbreak = 99
    installed: list[int] = []
    graceful: list[int] = []

    def _capture_signal(signum: int, _handler: object) -> signal.Handlers:
        installed.append(signum)
        return signal.SIG_DFL

    def _default_handler(_signum: int) -> signal.Handlers:
        return signal.SIG_DFL

    monkeypatch.setattr(
        cli,
        "_LIVE_TERMINATION_SIGNALS",
        (signal.SIGINT, signal.SIGTERM, sigbreak),
    )
    monkeypatch.setattr(signal, "getsignal", _default_handler)
    monkeypatch.setattr(signal, "signal", _capture_signal)
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    with guard:
        guard._handle(sigbreak, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        guard._handle(sigbreak, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert installed[:3] == [signal.SIGINT, signal.SIGTERM, sigbreak]
    assert graceful == [sigbreak, sigbreak]
    assert guard.received_signal == sigbreak
    with pytest.raises(SystemExit) as exc:
        cli._propagate_live_termination(sigbreak)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code == 128 + sigbreak


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_live_signal_guard_prebind_falls_back_to_previous_handler(signum: int) -> None:
    """A startup signal before Uvicorn binds still reaches the prior handler."""
    delegated: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard._previous[signum] = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        lambda received, _frame: delegated.append(received)
    )

    guard._handle(signum, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert delegated == [signum]


def test_live_signal_guard_prebind_sigterm_preserves_default_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real non-callable SIG_DFL disposition cannot swallow early SIGTERM."""
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard._previous[signal.SIGTERM] = signal.SIG_DFL  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _forced_exit(code: int) -> None:
        raise _ForcedProcessExit(code)

    monkeypatch.setattr(os, "_exit", _forced_exit)
    with pytest.raises(_ForcedProcessExit) as exc:
        guard._handle(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            signal.SIGTERM, None
        )
    assert exc.value.code == 128 + signal.SIGTERM


def test_live_signal_guard_prebind_sigint_ignores_noncallable_previous_handler() -> None:
    """A default pre-bind SIGINT records shutdown without calling a sentinel."""
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard._previous[signal.SIGINT] = signal.SIG_DFL  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    guard._handle(signal.SIGINT, None)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    assert guard.received_signal == signal.SIGINT


def test_live_signal_guard_exit_skips_missing_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoration tolerates a platform reporting no prior signal handler."""
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    guard._previous[signal.SIGINT] = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def _unexpected_restore(_signum: int, _handler: object) -> None:
        pytest.fail("a missing prior handler must not be restored")

    monkeypatch.setattr(signal, "signal", _unexpected_restore)

    guard.__exit__()


def test_live_signal_guard_replays_sigint_from_handler_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGINT during ``signal.signal`` is not lost before graceful bind."""
    prior_calls: list[int] = []
    graceful: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]
    raised = False

    def prior(signum: int, _frame: object) -> None:
        prior_calls.append(signum)

    def _get_prior(_signum: int) -> Callable[[int, object], None]:
        return prior

    monkeypatch.setattr(signal, "getsignal", _get_prior)

    def _install(signum: int, handler: object) -> Callable[[int, object], None]:
        nonlocal raised
        if signum == signal.SIGINT and getattr(handler, "__self__", None) is guard and not raised:
            raised = True
            handler(signum, None)  # type: ignore[operator]
        return prior

    monkeypatch.setattr(signal, "signal", _install)

    with guard:
        guard.bind_graceful_handler(lambda signum, _frame: graceful.append(signum))

    assert prior_calls == [signal.SIGINT]
    assert graceful == [signal.SIGINT]
    assert guard.received_signal == signal.SIGINT


def test_live_signal_guard_restores_handlers_when_installation_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside handler installation cannot leave our guard exposed."""
    restored: list[int] = []
    guard = cli._LiveSignalGuard()  # pyright: ignore[reportPrivateUsage]

    def _prior(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def _get_prior(_signum: int) -> Callable[[int, object], None]:
        return _prior

    monkeypatch.setattr(signal, "getsignal", _get_prior)

    def _install(signum: int, handler: object) -> Callable[[int, object], None]:
        if getattr(handler, "__self__", None) is guard:
            handler(signum, None)  # type: ignore[operator]
        else:
            restored.append(signum)
        return _prior

    monkeypatch.setattr(signal, "signal", _install)

    with pytest.raises(KeyboardInterrupt):
        guard.__enter__()

    assert restored == [signal.SIGINT]


def test_signal_managed_server_does_not_replace_process_handlers() -> None:
    """The Uvicorn override's context executes without installing handlers."""
    server = cli._SignalManagedServer(uvicorn.Config("tests.test_cli:app"))  # pyright: ignore[reportPrivateUsage]
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is before_int
        assert signal.getsignal(signal.SIGTERM) is before_term


def test_live_termination_propagates_conventional_exit_semantics() -> None:
    """Post-teardown SIGINT/SIGTERM translation is explicit and stable."""
    with pytest.raises(KeyboardInterrupt):
        cli._propagate_live_termination(signal.SIGINT)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SystemExit) as exc:
        cli._propagate_live_termination(signal.SIGTERM)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code == 128 + signal.SIGTERM
    assert cli._propagate_live_termination(None) is None  # pyright: ignore[reportPrivateUsage]


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
    from roastpilot_agent.replay import ReplaySource

    closed: list[ReplaySource] = []
    real_aclose = ReplaySource.aclose

    async def recording_aclose(source: ReplaySource) -> None:
        closed.append(source)
        await real_aclose(source)

    monkeypatch.setattr(ReplaySource, "aclose", recording_aclose)
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "replaying session-2" in out
    assert "stepped (paused at tick 0)" in out
    assert len(closed) == 1


def test_replay_closes_source_when_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serve failure closes replay resources without relying on lifespan."""
    import uvicorn

    from roastpilot_agent.replay import ReplaySource

    closed: list[ReplaySource] = []
    real_aclose = ReplaySource.aclose

    async def recording_aclose(source: ReplaySource) -> None:
        closed.append(source)
        await real_aclose(source)

    async def fail_serve(_server: uvicorn.Server) -> None:
        raise RuntimeError("serve failed")

    monkeypatch.setattr(ReplaySource, "aclose", recording_aclose)
    monkeypatch.setattr(uvicorn.Server, "serve", fail_serve)
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )

    with pytest.raises(RuntimeError, match="serve failed"):
        cli.main()
    assert len(closed) == 1


def test_replay_source_failure_cancels_server_and_closes_source(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replay failure stays primary when both server cancellation and close fail."""
    import uvicorn

    from roastpilot_agent.replay import ReplaySource

    closed: list[ReplaySource] = []
    server_cancelled: list[bool] = []
    real_aclose = ReplaySource.aclose

    async def recording_aclose(source: ReplaySource) -> None:
        closed.append(source)
        await real_aclose(source)
        raise RuntimeError("close failed")

    async def fail_run(_source: ReplaySource) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("source failed")

    async def pending_serve(_server: uvicorn.Server) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            server_cancelled.append(True)
            raise RuntimeError("server cancellation failed") from None

    monkeypatch.setattr(ReplaySource, "aclose", recording_aclose)
    monkeypatch.setattr(ReplaySource, "run", fail_run)
    monkeypatch.setattr(uvicorn.Server, "serve", pending_serve)
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--speed", "60", "--port", "0"],
    )

    with pytest.raises(RuntimeError, match="source failed"):
        cli.main()
    assert server_cancelled == [True]
    assert len(closed) == 1
    assert "close failed" in caplog.text


def test_replay_close_failure_propagates_without_a_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's handled exception does not hide a standalone close failure."""
    import uvicorn

    from roastpilot_agent.replay import ReplaySource

    real_aclose = ReplaySource.aclose

    async def fail_aclose(source: ReplaySource) -> None:
        await real_aclose(source)
        raise RuntimeError("close failed")

    async def no_op_serve(_server: uvicorn.Server) -> None:
        return None

    monkeypatch.setattr(ReplaySource, "aclose", fail_aclose)
    monkeypatch.setattr(uvicorn.Server, "serve", no_op_serve)
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )

    try:
        raise ValueError("caller's handled error")
    except ValueError:
        with pytest.raises(RuntimeError, match="close failed"):
            cli.main()


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
def test_serve_arms_exit_guard_after_unexpected_build_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected post-cleanup build failure still bounds finalization."""
    from roastpilot_agent import live

    calls: list[str] = []

    class _Guard:
        def arm(self, _labels: tuple[str, ...]) -> None:
            calls.append("arm")

        def disarm(self) -> None:
            calls.append("disarm")

    async def _boom(config: object, *, store_path: object) -> object:  # noqa: ANN401
        raise RuntimeError("unexpected build failure after owned cleanup")

    monkeypatch.setattr(cli, "_LiveExitGuard", _Guard)
    monkeypatch.setattr(live, "build_live_service", _boom)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])

    with pytest.raises(RuntimeError, match="unexpected build failure"):
        cli.main()

    assert calls == ["arm", "disarm"]


@pytest.mark.usefixtures("no_serve")
def test_serve_restores_signal_handlers_before_final_termination_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last sticky-signal read occurs only after handler restoration."""
    order: list[str] = []

    class _ExitGuard:
        def disarm(self) -> None:
            order.append("disarm")

    class _SignalGuard:
        received_signal: int | None = None

        def __enter__(self) -> "_SignalGuard":
            order.append("enter")
            return self

        def __exit__(self, *_args: object) -> None:
            order.append("restore")

    def _run(coroutine: Coroutine[object, object, int]) -> int:
        order.append("run")
        coroutine.close()
        return 0

    def _propagate(_signum: int | None) -> None:
        order.append("final-check")

    monkeypatch.setattr(cli, "_LiveExitGuard", _ExitGuard)
    monkeypatch.setattr(cli, "_LiveSignalGuard", _SignalGuard)
    monkeypatch.setattr(asyncio, "run", _run)
    monkeypatch.setattr(cli, "_propagate_live_termination", _propagate)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])

    assert cli.main() == 0
    assert order == ["enter", "run", "restore", "final-check", "disarm"]


@pytest.mark.usefixtures("no_serve")
def test_serve_propagates_signal_received_during_handler_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM in ``__exit__`` is observed by the post-guard check."""
    original_guard = cli._LiveSignalGuard  # pyright: ignore[reportPrivateUsage]

    class _RestorationSignalGuard(original_guard):
        def __exit__(self, *_args: object) -> None:
            signal.raise_signal(signal.SIGTERM)
            super().__exit__(*_args)

    async def _serve(
        _args: object,
        *,
        exit_guard: object,
        signal_guard: _RestorationSignalGuard,
    ) -> int:
        del exit_guard
        signal_guard.bind_graceful_handler(lambda _signum, _frame: None)
        return 0

    monkeypatch.setattr(cli, "_LiveSignalGuard", _RestorationSignalGuard)
    monkeypatch.setattr(cli, "_serve_live", _serve)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 128 + signal.SIGTERM


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

        async def _tracked_heat_off() -> bool:
            order.append("heat-off")
            return True

        async def _tracked_shutdown() -> None:
            order.append("service.shutdown")
            await original_shutdown()

        monkeypatch.setattr(service, "safe_shutdown_heat_off", _tracked_heat_off)
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
    assert order == ["heat-off", "service.shutdown", "mcp.stop", "store.close"]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_serve_first_signal_tears_down_then_propagates(
    signum: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real first signal crosses the live object graph and exits after teardown."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    order: list[str] = []

    class _FakeMCP:
        running = True
        stop_unconfirmed = False
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
        store = _RecordingStore(store_path)
        service = RoastService(store)
        original_shutdown = service.shutdown

        async def _tracked_heat_off() -> bool:
            order.append("heat-off")
            return True

        async def _tracked_shutdown() -> None:
            order.append("service.shutdown")
            await original_shutdown()

        monkeypatch.setattr(service, "safe_shutdown_heat_off", _tracked_heat_off)
        monkeypatch.setattr(service, "shutdown", _tracked_shutdown)
        return service, _FakeMCP(), store

    async def _raise_first_signal(
        _server: cli._SignalManagedServer,  # pyright: ignore[reportPrivateUsage]
        _sockets: object = None,
    ) -> None:
        signal.raise_signal(signum)

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr(cli._SignalManagedServer, "_serve", _raise_first_signal)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "serve", "--port", "0", "--db", str(tmp_path / "live.sqlite3")],
    )

    if signum == signal.SIGINT:
        with pytest.raises(KeyboardInterrupt):
            cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 128 + signal.SIGTERM
    assert order == ["heat-off", "service.shutdown", "mcp.stop", "store.close"]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_serve_startup_signal_tears_down_then_translates_cancellation(
    signum: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-Uvicorn signal translates cancellation after ordered teardown."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    order: list[str] = []

    class _FakeMCP:
        running = True
        stop_unconfirmed = False
        call_tool = staticmethod(_make_call_tool())

        async def stop(self) -> None:
            order.append("mcp.stop")

    class _SignalDuringInitializeStore(RoastStore):
        async def initialize(self) -> None:
            signal.raise_signal(signum)
            await asyncio.sleep(0)

        async def close(self) -> None:
            order.append("store.close")

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        store = _SignalDuringInitializeStore(store_path)
        service = RoastService(store)
        original_shutdown = service.shutdown

        async def _tracked_heat_off() -> bool:
            order.append("heat-off")
            return True

        async def _tracked_shutdown() -> None:
            order.append("service.shutdown")
            await original_shutdown()

        monkeypatch.setattr(service, "safe_shutdown_heat_off", _tracked_heat_off)
        monkeypatch.setattr(service, "shutdown", _tracked_shutdown)
        return service, _FakeMCP(), store

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "serve", "--port", "0", "--db", str(tmp_path / "live.sqlite3")],
    )

    if signum == signal.SIGINT:
        with pytest.raises(KeyboardInterrupt):
            cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 128 + signal.SIGTERM
    assert order == ["heat-off", "service.shutdown", "mcp.stop", "store.close"]


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


@pytest.mark.asyncio
async def test_live_teardown_resists_repeated_cancellation() -> None:
    """First/repeated cancellation cannot interrupt ordered live teardown."""
    order: list[str] = []
    heat_started = asyncio.Event()
    allow_heat_done = asyncio.Event()

    class _Service:
        async def safe_shutdown_heat_off(self) -> None:
            heat_started.set()
            await allow_heat_done.wait()
            order.append("heat-off")

        async def shutdown(self) -> None:
            order.append("service-stop")

        async def record_child_stop_unconfirmed(self, *, stop_unconfirmed: bool) -> None:
            order.append(f"unconfirmed:{stop_unconfirmed}")

    class _MCP:
        stop_unconfirmed = True

        async def stop(self) -> None:
            order.append("mcp-stop")

    class _Store:
        async def close(self) -> None:
            order.append("store-close")

    teardown = asyncio.create_task(
        cli._finish_live_teardown(  # pyright: ignore[reportPrivateUsage]
            _Service(),  # type: ignore[arg-type]
            _MCP(),  # type: ignore[arg-type]
            _Store(),  # type: ignore[arg-type]
            None,
        )
    )
    await heat_started.wait()
    teardown.cancel()
    await asyncio.sleep(0)
    teardown.cancel()
    allow_heat_done.set()

    with pytest.raises(asyncio.CancelledError):
        await teardown
    assert order == [
        "heat-off",
        "service-stop",
        "mcp-stop",
        "unconfirmed:True",
        "store-close",
    ]


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


def test_format_post_fc_loop_readout_enabled() -> None:
    """Loop enabled prints a can't-miss ⚠️ line naming #405 (issue #460), and
    (#498/D89) is explicit that the division is heat-deterministic/fan-advisor
    — never implies fan is ALSO pinned/deterministic, which would misdescribe
    the current actuation split to the operator. Names the taper's write as
    the mechanism (safety-reviewer LOW-1): the advisor's fan JUDGMENT is
    applied BY the taper's single write (#498's coalesced-writer design), not
    a claim that the advisor writes to the roaster directly."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=True, ceiling_guard_enabled=False, ceiling_guard_temp_c=196.0
    )
    text = "\n".join(lines)
    assert "⚠️" in text
    assert "POST-FC RoR LOOP: ENABLED" in text
    assert "#405" in text
    assert "#498" in text
    assert "advisor's fan judgment" in text
    assert "taper's write" in text


def test_format_post_fc_loop_readout_disabled() -> None:
    """Both flags off (the default) prints quiet, non-alarming lines only."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=False, ceiling_guard_enabled=False, ceiling_guard_temp_c=196.0
    )
    text = "\n".join(lines)
    assert "⚠️" not in text
    assert "post-FC RoR loop: disabled" in text
    assert "advisor-driven post-FC" in text
    assert "ceiling-guard drop: disabled" in text


def test_format_post_fc_loop_readout_ceiling_guard_enabled() -> None:
    """Guard enabled prints its own ⚠️ line with the RESOLVED guard temperature,
    independent of the loop flag (D88 decoupling, issue #495)."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=False, ceiling_guard_enabled=True, ceiling_guard_temp_c=196.0
    )
    text = "\n".join(lines)
    assert "CEILING-GUARD DROP: ENABLED" in text
    assert "196 °C" in text
    # The guard line is loud even while the loop line stays quiet — the flags
    # are independent and each must be confirmable on its own.
    assert "post-FC RoR loop: disabled" in text
    assert text.count("⚠️") == 1


def test_format_post_fc_loop_readout_both_enabled_two_loud_lines() -> None:
    """The full D88 treatment arm (taper + guard, recovery still at its
    default OFF) prints TWO ⚠️ lines — one per D88 flag — so the operator
    confirms each independently before charging beans. The THIRD (D96
    recovery) line is always present (#559/PR #560 round 3) but stays quiet
    here since ``recovery_enabled`` was not passed (defaults ``False``)."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=True, ceiling_guard_enabled=True, ceiling_guard_temp_c=195.5
    )
    assert len(lines) == 3
    text = "\n".join(lines)
    assert text.count("⚠️") == 2
    assert "POST-FC RoR LOOP: ENABLED" in text
    assert "CEILING-GUARD DROP: ENABLED" in text
    # The temperature shown is the resolved value, not a hardcoded default.
    assert "195.5 °C" in text
    assert "bidirectional heat recovery: disabled" in text


def test_format_post_fc_loop_readout_recovery_enabled_shows_cap() -> None:
    """D96 (#559/PR #560 round 3): recovery ENABLED prints its own loud ⚠️
    line naming D96 and the RESOLVED headroom cap (not a hardcoded default)
    — the operator must be able to confirm the recovery state (both a
    typo'd-OFF and an accidental-ON) and the actual raise ceiling from the
    can't-miss startup readout, independent of the two D88 flags."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=True,
        ceiling_guard_enabled=True,
        ceiling_guard_temp_c=196.0,
        recovery_enabled=True,
        recovery_headroom_percentage_points=15,
    )
    assert len(lines) == 3
    text = "\n".join(lines)
    assert text.count("⚠️") == 3
    assert "BOUNDED-BIDIRECTIONAL HEAT RECOVERY: ENABLED" in text
    assert "D96" in text
    # The headroom cap shown is the resolved value, not a hardcoded default.
    assert "15" in text
    assert "bidirectional heat recovery: disabled" not in text


def test_format_post_fc_loop_readout_recovery_disabled_by_default() -> None:
    """The default (no ``recovery_enabled`` kwarg passed) reads as disabled,
    quiet, and names D88's never-add-heat-beyond-entry law as still standing
    — the pre-D96 readout shape stays reachable for any caller that predates
    the recovery kwargs."""
    lines = cli._format_post_fc_loop_readout(  # pyright: ignore[reportPrivateUsage]
        enabled=False, ceiling_guard_enabled=False, ceiling_guard_temp_c=196.0
    )
    assert len(lines) == 3
    text = "\n".join(lines)
    assert "bidirectional heat recovery: disabled" in text
    assert "D88 never-add-heat-beyond-entry stands" in text
    assert "BOUNDED-BIDIRECTIONAL" not in text


@pytest.mark.usefixtures("no_serve")
def test_serve_live_banner_reflects_resolved_d88_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The D88 banner lines come from the RESOLVED config the serving agent
    loaded — end-to-end through the real ``serve`` path, not the pure readout
    function (issues #460/#495).

    Sets the ceiling-guard flag ON (plus a non-default guard temperature) and
    the RoR-taper loop flag explicitly OFF (12 Jul promotion flipped its
    config-field default to True — a real "baseline arm" run must set this
    itself, matching the new ``POST_FC_LOOP=0`` toggle in
    ``scripts/roast-live.sh``) through the real nested env vars, then drives
    ``cli.main()``: the guard line must be loud with the resolved temperature
    while the loop line stays quiet. Asymmetric on purpose — the two flags are
    structurally identical bools, so a swapped-kwarg bug at the
    ``_serve_live`` call site would flip BOTH assertions; the pure-function
    tests above can never see that wiring."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__ENABLED",
        "false",
    )
    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_DROP_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_TEMP_C",
        "195.5",
    )

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text("<title>RoastPilot</title>", encoding="utf-8")

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool(_runtime_config_payload(roaster_driver="mock")))

        async def stop(self) -> None:
            return None

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        store = RoastStore(store_path)
        return RoastService(store), _FakeMCP(), store

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr(
        "sys.argv",
        [
            "roastpilot-agent",
            "serve",
            "--port",
            "0",
            "--spa-dir",
            str(spa),
            "--db",
            str(tmp_path / "trace" / "live.sqlite3"),
        ],
    )

    assert cli.main() == 0
    out = capsys.readouterr().out
    # Guard ON: its loud line prints with the RESOLVED (non-default) temperature.
    assert "CEILING-GUARD DROP: ENABLED" in out
    assert "195.5 °C" in out
    # Loop OFF: its line stays quiet. Swapped caller wiring fails both checks.
    assert "post-FC RoR loop: disabled" in out
    assert "POST-FC RoR LOOP: ENABLED" not in out
    # D96 (#559/PR #560 round 3): recovery was never set via env here, so it
    # stays at its default OFF — the third line prints quiet, not silently
    # omitted (an operator must see the line exists to know the flag was
    # actually read, not just absent from an older binary).
    assert "bidirectional heat recovery: disabled" in out
    assert "BOUNDED-BIDIRECTIONAL" not in out


@pytest.mark.usefixtures("no_serve")
def test_serve_live_banner_reflects_resolved_recovery_flag_and_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D96 (#559/PR #560 round 3): the recovery banner line comes from the
    RESOLVED config the serving agent loaded — end-to-end through the real
    ``serve`` path, not the pure readout function, mirroring
    ``test_serve_live_banner_reflects_resolved_d88_flags`` exactly for the
    THIRD flag. Sets recovery ON with a non-default headroom cap (the
    ceiling-guard flag must ALSO be on — the D96 cross-field validator
    requires it) through the real nested env vars: the recovery line must be
    loud with the resolved cap."""
    from roastpilot_agent import live
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.store import RoastStore

    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_DROP_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__RECOVERY_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__RECOVERY_HEADROOM_PERCENTAGE_POINTS",
        "22",
    )

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text("<title>RoastPilot</title>", encoding="utf-8")

    class _FakeMCP:
        running = True
        call_tool = staticmethod(_make_call_tool(_runtime_config_payload(roaster_driver="mock")))

        async def stop(self) -> None:
            return None

    async def _fake_build(
        config: AppConfig, *, store_path: Path
    ) -> tuple[RoastService, _FakeMCP, RoastStore]:
        store = RoastStore(store_path)
        return RoastService(store), _FakeMCP(), store

    monkeypatch.setattr(live, "build_live_service", _fake_build)
    monkeypatch.setattr(
        "sys.argv",
        [
            "roastpilot-agent",
            "serve",
            "--port",
            "0",
            "--spa-dir",
            str(spa),
            "--db",
            str(tmp_path / "trace" / "live.sqlite3"),
        ],
    )

    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "BOUNDED-BIDIRECTIONAL HEAT RECOVERY: ENABLED" in out
    assert "22" in out
    assert "bidirectional heat recovery: disabled" not in out


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


def test_serve_live_returns_error_on_malformed_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``serve`` returns exit-code 1 and prints an error when the saved-config
    file is malformed (``ConfigFileError`` from ``load_app_config``).

    The MCP child must NOT be started — ``load_app_config`` runs before
    ``build_live_service``, so a bad config file is a fail-closed startup error.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(": broken: yaml\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    # Ensure build_live_service is never called (would need a real MCP child).
    import roastpilot_agent.live as _live

    def _should_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build_live_service must not be called on config error")

    monkeypatch.setattr(_live, "build_live_service", _should_not_be_called)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "malformed" in out or "error" in out


def test_replay_returns_error_on_malformed_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay`` returns exit-code 1 with a clear message when the saved-config
    file is malformed (``ConfigFileError`` from ``load_app_config``).

    After the resource-leak fix (Codex P2, PR #425), config is loaded BEFORE
    ``create_replay_app`` so the aiosqlite worker / ReplaySource are never
    allocated on a bad config — verified by the companion test
    ``test_replay_config_error_does_not_allocate_replay_resources``.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(": broken: yaml\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "malformed" in out or "error" in out


def test_serve_live_returns_error_on_schema_invalid_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``serve`` returns exit-code 1 with an error message when the saved-config
    file is valid YAML but violates the schema (``ValidationError``).

    Distinct from the malformed-YAML case: the file parses as YAML but Pydantic
    rejects the value (e.g. a string where a number is required).  Both
    ``ConfigFileError`` and ``pydantic.ValidationError`` must produce a clean
    error message rather than a raw traceback (Codex P2 finding, PR #425).
    """
    cfg_path = tmp_path / "config.yaml"
    # Valid YAML structure, but tick_interval_seconds must be a float — a string
    # that cannot be coerced is a schema violation raising ValidationError, not
    # ConfigFileError.
    cfg_path.write_text(
        "advisor:\n  timeout_seconds: 'not_a_number'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    import roastpilot_agent.live as _live

    def _should_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build_live_service must not be called on config error")

    monkeypatch.setattr(_live, "build_live_service", _should_not_be_called)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "invalid" in out or "error" in out


def test_replay_returns_error_on_schema_invalid_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay`` returns exit-code 1 with a clear message when the saved-config
    file is valid YAML but fails schema validation (``ValidationError``).

    Mirrors the live-serve case for the replay path (Codex P2 finding, PR #425).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "advisor:\n  timeout_seconds: 'not_a_number'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "invalid" in out or "error" in out


def test_replay_config_error_does_not_allocate_replay_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad saved-config file in ``--replay`` returns 1 without ever calling
    ``create_replay_app``.

    Before the resource-leak fix (Codex P2, PR #425), config was loaded AFTER
    ``create_replay_app`` so aiosqlite + ReplaySource were allocated even on a
    bad config.  After the fix, config is loaded first and a bad config
    short-circuits before any replay resource is allocated.

    The test patches ``create_replay_app`` to raise ``AssertionError`` if
    called, proving the allocation never happens.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(": broken: yaml\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    import roastpilot_agent.replay as _replay

    def _must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("create_replay_app must not be called when config fails")

    monkeypatch.setattr(_replay, "create_replay_app", _must_not_be_called)

    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "malformed" in out or "error" in out


def test_serve_live_returns_error_on_unreadable_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``serve`` returns exit-code 1 with a clear message when the saved-config
    file exists but cannot be read (``OSError`` from ``load_app_config``).

    Distinct from the malformed/schema-invalid cases: the file is syntactically
    valid but unreadable (e.g. permissions).  All three error classes must
    produce a clean fail-closed startup error rather than a raw traceback
    (claude-review low, PR #425).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("advisor:\n  model_slug: openai/gpt-4o\n", encoding="utf-8")
    cfg_path.chmod(0o000)  # remove all permissions → OSError on open
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    import roastpilot_agent.live as _live

    def _should_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build_live_service must not be called on config error")

    monkeypatch.setattr(_live, "build_live_service", _should_not_be_called)
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "serve", "--port", "0"])
    try:
        result = cli.main()
    finally:
        cfg_path.chmod(0o644)  # restore so tmp_path cleanup can delete it
    assert result == 1
    out = capsys.readouterr().out
    assert "unreadable" in out or "error" in out


def test_replay_returns_error_on_unreadable_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--replay`` returns exit-code 1 with a clear message when the saved-config
    file is unreadable (``OSError`` from ``load_app_config``).

    Mirrors ``test_serve_live_returns_error_on_unreadable_config_file`` for the
    replay path (claude-review low, PR #425).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("advisor:\n  model_slug: openai/gpt-4o\n", encoding="utf-8")
    cfg_path.chmod(0o000)
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))

    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    try:
        result = cli.main()
    finally:
        cfg_path.chmod(0o644)
    assert result == 1
    out = capsys.readouterr().out
    assert "unreadable" in out or "error" in out


@pytest.mark.usefixtures("no_serve")
def test_replay_passes_saved_config_to_create_replay_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--replay`` passes the loaded saved-config to ``create_replay_app``.

    Before the fix, ``_cfg`` was loaded above ``create_replay_app`` (to close
    the resource-leak) but the ``config=`` kwarg was never forwarded — so
    ``build_replay_service`` fell back to ``AppConfig()`` (schema defaults),
    and the replay service ran on different config than ``GET /api/config``
    showed (saved values diverged from the running service).

    The test spies on ``create_replay_app`` to capture the ``config=`` kwarg
    and asserts it reflects the non-default advisor model_slug written to the
    saved-config file (claude-review medium, PR #425).
    """
    import roastpilot_agent.replay as _replay
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.config_store import (
        AdvisorConfigEdit,
        AppConfigEdit,
        persist_config_edit,
    )

    # Write a saved config with a recognisable non-default value.
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))
    # Clear any stray ROASTPILOT_ADVISOR__MODEL_SLUG env var so env does not win.
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    captured: list[AppConfig] = []
    _real_create_replay_app = _replay.create_replay_app

    async def _spy_create_replay_app(
        export_dir: Path,
        store_path: Path,
        *,
        config: AppConfig | None = None,
        **kwargs: object,
    ) -> object:
        captured.append(config)  # type: ignore[arg-type]
        return await _real_create_replay_app(
            export_dir,
            store_path,
            config=config,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(_replay, "create_replay_app", _spy_create_replay_app)

    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--step", "--port", "0"],
    )
    assert cli.main() == 0

    assert len(captured) == 1, "create_replay_app must have been called exactly once"
    assert captured[0] is not None, "config= kwarg must not be None"
    assert captured[0].advisor.model_slug == "openai/gpt-4o-mini", (
        "replay service must use the saved-file config value, not the schema default"
    )
