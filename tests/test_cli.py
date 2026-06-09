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
