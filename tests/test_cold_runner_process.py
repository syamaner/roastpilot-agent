"""Process tests for the cold runner with real ``os._exit``, signals and uvicorn (#954 U4).

Each test spawns ``tests/cold_runner_driver.py`` as a child process (fake cold run,
loopback ephemeral port, ``TMPDIR`` under ``tmp_path``) and signals only that child.
Every read of the child's output is bounded; a bound expiring is a test failure,
never guard proof.  No MCP child, provider, serial port or ``/proc`` read is used.
"""

import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import typing
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.serial(reason="drives a real child process with real signals and os._exit"),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
BOUND_SECONDS = 60.0
MARKER = "SECRETMARKER5b2a"
MODE_LINE = "roastpilot-agent cold-characterisation: run starting\n"


class Driver:
    """One spawned driver with bounded line reads from its stdout."""

    def __init__(self, mode: str, tmp_path: Path) -> None:
        spa = tmp_path / "spa"
        spa.mkdir()
        (spa / "index.html").write_text("<html>driver</html>", encoding="utf-8")
        self.temp_root = tmp_path / "tmp"
        self.temp_root.mkdir()
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("ROASTPILOT_", "COFFEE_"))
            and name not in {"OPENROUTER_API_KEY", "PYTHONPATH"}
        }
        env["TMPDIR"] = str(self.temp_root)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.cold_runner_driver", mode, str(spa)],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        for line in stdout:
            self._queue.put(line)
        self._queue.put(None)

    def expect(self, wanted: str) -> str:
        """Read lines until one equals ``wanted`` (or, ending in ``*``, starts with it)."""
        while True:
            try:
                line = self._queue.get(timeout=BOUND_SECONDS)
            except queue.Empty:
                pytest.fail(f"bound expired waiting for {wanted!r}")
            if line is None:
                pytest.fail(f"driver output ended before {wanted!r}")
            self.lines.append(line)
            text = line.rstrip("\n")
            if text == wanted or (wanted.endswith("*") and text.startswith(wanted[:-1])):
                return text

    def observe(self, wanted: str) -> bool:
        """Read lines until ``wanted``; ``False`` if the driver's output ended first.

        A bound expiring is still a test failure (never guard proof); only the
        driver's own exit is reported, so the caller asserts on it explicitly.
        """
        while True:
            try:
                line = self._queue.get(timeout=BOUND_SECONDS)
            except queue.Empty:
                pytest.fail(f"bound expired waiting for {wanted!r}")
            if line is None:
                return False
            self.lines.append(line)
            if line.rstrip("\n") == wanted:
                return True

    def exit_code(self) -> int:
        """The driver's exit code once it has ended (bounded wait)."""
        try:
            return self.process.wait(timeout=BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("bound expired waiting for the driver to exit")

    def signal(self, signum: int) -> None:
        self.process.send_signal(signum)

    def release(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write("release\n")
        stdin.flush()

    def finish(self) -> tuple[int, str, str]:
        """Wait (bounded) for exit; return the code, all stdout and all stderr."""
        try:
            code = self.process.wait(timeout=BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("bound expired waiting for the driver to exit")
        self._reader.join(timeout=BOUND_SECONDS)
        while True:
            item = self._queue.get_nowait() if not self._queue.empty() else None
            if item is None:
                break
            self.lines.append(item)
        stderr = self.process.stderr
        assert stderr is not None
        return code, "".join(self.lines), stderr.read()

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=BOUND_SECONDS)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def spawn(tmp_path: Path) -> typing.Iterator[typing.Callable[[str], Driver]]:
    drivers: list[Driver] = []

    def start(mode: str) -> Driver:
        driver = Driver(mode, tmp_path)
        drivers.append(driver)
        return driver

    yield start
    for driver in drivers:
        driver.kill()


def summary_of(stdout: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in stdout.splitlines()
        if "=" in line and not line.startswith(("SIG ", "CANCELLING "))
    )


def test_p1_pending_exit_is_direct_output_free_and_leaves_private_directories(
    spawn: typing.Callable[[str], Driver],
) -> None:
    """P1: real ``os._exit(80)``; stdout is the mode line only; both temp dirs remain."""
    driver = spawn("pending")
    code, stdout, _stderr = driver.finish()
    assert code == 80
    assert stdout == MODE_LINE
    names = [path.name for path in driver.temp_root.iterdir()]
    assert any(name.startswith("roastpilot-cold-store-") for name in names)
    assert any(
        name.startswith("roastpilot-cold-") and not name.startswith("roastpilot-cold-store-")
        for name in names
    )


def test_p2_repeated_signals_never_force_exit_or_recancel(
    spawn: typing.Callable[[str], Driver],
) -> None:
    """P2: first SIGINT cancels once; SIGINT+SIGTERM during cleanup only record."""
    driver = spawn("signals")
    driver.expect("READY")
    driver.signal(signal.SIGINT)
    driver.expect(f"SIG {int(signal.SIGINT)}")
    driver.expect("CANCELLED")
    driver.signal(signal.SIGINT)
    survived = driver.observe(f"SIG {int(signal.SIGINT)}")
    assert survived, f"a repeated SIGINT ended the driver with exit {driver.exit_code()}"
    driver.signal(signal.SIGTERM)
    survived = driver.observe(f"SIG {int(signal.SIGTERM)}")
    assert survived, f"a repeated SIGTERM ended the driver with exit {driver.exit_code()}"
    assert driver.process.poll() is None
    driver.release()
    code, stdout, _stderr = driver.finish()
    assert code == 130
    assert "CANCELLING 1\n" in stdout
    summary = summary_of(stdout)
    assert summary["result"] == "cancelled"
    assert summary["run_invoked"] == "true"
    assert summary["child_ownership"] == "unknown"
    assert summary["signal"] == "sigint"
    assert summary["exit_code"] == "130"


def test_p3_real_uvicorn_never_logs_request_detail(
    spawn: typing.Callable[[str], Driver],
) -> None:
    """P3: planted request text never reaches the child's stdout or stderr."""
    import httpx

    driver = spawn("http")
    port = int(driver.expect("PORT *").split()[1])
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=BOUND_SECONDS) as client:
        with client.stream(
            "GET",
            base + "/api/cold-characterisation/events",
            params={"last_event_id": MARKER},
        ) as events:
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            first = next(events.iter_text())
            assert first.startswith(": connected")
        head = client.head(
            base + "/api/cold-characterisation/events", params={"last_event_id": MARKER}
        )
        assert head.status_code == 200
        missing = client.get(base + "/api/not-a-route", params={"x": MARKER})
        assert missing.status_code == 404
        blocked = client.post(base + "/", content=MARKER.encode())
        assert blocked.status_code == 409
    with socket.create_connection(("127.0.0.1", port), timeout=BOUND_SECONDS) as raw:
        raw.sendall(f"GET /{MARKER} HTTP/1.1 {MARKER}\r\n\r\n".encode())
        raw.recv(4096)
    driver.release()
    code, stdout, stderr = driver.finish()
    assert code == 6
    assert MARKER not in stdout
    assert MARKER not in stderr
    assert "cold-http WARNING" in stderr


def test_p4_sigterm_before_the_run_prevents_it(spawn: typing.Callable[[str], Driver]) -> None:
    """P4: SIGTERM while the store initialises means exit 143 and no run."""
    driver = spawn("pre_sigterm")
    driver.expect("READY_PRE")
    driver.signal(signal.SIGTERM)
    code, stdout, _stderr = driver.finish()
    assert code == 143
    summary = summary_of(stdout)
    assert summary["result"] == "cancelled_before_run"
    assert summary["run_invoked"] == "false"
    assert summary["signal"] == "sigterm"
    assert MODE_LINE not in stdout
