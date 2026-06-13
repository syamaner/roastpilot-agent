"""CLI tests (E10-S1): argument parsing + the ``--replay`` serve dispatch.

Hardware-free and server-free: the serve path is exercised with uvicorn's
``Server.serve`` patched to a no-op, so the replay app is built and the run is
driven without binding a socket.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from roastpilot_agent import cli


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
    completion via run() — the banner reports the free-running mode. The fault
    fixture is used (9 frames) so the inter-tick sleeps stay negligible."""
    fixture = Path(__file__).parent / "fixtures" / "replay" / "fault-pre-t0"
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "--replay", str(fixture), "--speed", "60", "--port", "0"],
    )
    assert cli.main() == 0
    assert "free-running at 60x" in capsys.readouterr().out


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
