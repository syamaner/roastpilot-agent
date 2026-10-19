"""Fresh-process lifecycle regression tests."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, NoReturn

import pytest

from roastpilot_agent.config import DEFAULT_MCP_COMMAND
from roastpilot_agent.mcp_client import resolve_mcp_command

pytestmark = pytest.mark.serial(reason="drives POSIX process groups with killpg")

_IMPORT_READY_BUDGET_SECONDS: Final = 30.0
_LIFECYCLE_BUDGET_SECONDS: Final = 30.0
_READY_POLL_SECONDS: Final = 0.05
_POST_KILL_COLLECT_SECONDS: Final = 5.0


def _kill_process_group(pid: int) -> None:
    """Best-effort kill one POSIX process group."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _kill_captured_process_group(pid_file: Path) -> None:
    """Kill one independently-sessioned process recorded in a pid file."""
    with contextlib.suppress(OSError, ValueError):
        _kill_process_group(int(pid_file.read_text().strip()))


def _kill_probe_process_groups(probe_pid: int, mcp_pid_file: Path) -> None:
    """Kill the independently-sessioned MCP child and its outer probe."""
    _kill_captured_process_group(mcp_pid_file)
    _kill_process_group(probe_pid)


def _run_phased_lifecycle_probe(
    argv: list[str],
    *,
    ready_marker: Path,
    mcp_pid_file: Path,
    import_budget_seconds: float = _IMPORT_READY_BUDGET_SECONDS,
    lifecycle_budget_seconds: float = _LIFECYCLE_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[subprocess.Popen[str], str, str]:
    """Run a fresh-interpreter probe under a readiness phase then a lifecycle phase.

    The child must atomically publish ``ready <pid>\\n`` at ``ready_marker`` within
    ``import_budget_seconds`` of spawn; the lifecycle budget is then anchored at the
    last clock reading that saw the marker absent, so it precedes publication and
    total observation stays below ``import + lifecycle`` before bounded cleanup.

    Args:
        argv: Child command line.
        ready_marker: Fresh path the child publishes once its real imports finish.
        mcp_pid_file: File the child writes its independently-sessioned MCP PID to.
        import_budget_seconds: Spawn-to-readiness bound.
        lifecycle_budget_seconds: Readiness-to-exit bound.
        clock: Non-decreasing clock never below real elapsed time.

    Returns:
        The reaped child plus its complete stdout and stderr (a nonzero exit is a return).
    """
    staging = ready_marker.with_name(ready_marker.name + ".tmp")
    t0 = clock()
    if ready_marker.exists() or staging.exists():
        pytest.fail(f"stale readiness marker before spawn: {ready_marker}")
    last_absent_at = t0
    import_deadline = t0 + import_budget_seconds
    child = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout = stderr = ""
    exited = False
    cleaned = False
    cleanup_errors: list[Exception] = []

    def _cleanup_once() -> bool:
        """Kill owned groups and collect output at most once; False if still running."""
        nonlocal cleaned, exited, stdout, stderr
        if cleaned:
            return True
        cleaned = True
        try:
            _kill_captured_process_group(mcp_pid_file)
        except Exception as error:
            cleanup_errors.append(error)
        try:
            if child.poll() is None:
                _kill_process_group(child.pid)
        except Exception as error:
            cleanup_errors.append(error)
        if exited:
            return True
        try:
            stdout, stderr = child.communicate(timeout=_POST_KILL_COLLECT_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        exited = True
        return True

    def _fail(message: str) -> NoReturn:
        collected = _cleanup_once()
        context = "".join(f"\ncleanup error: {error!r}" for error in cleanup_errors)
        if not collected:
            pytest.fail(
                f"lifecycle probe did not exit after both groups were killed; {message}{context}"
            )
        pytest.fail(f"{message}{context}\nstdout:\n{stdout}\nstderr:\n{stderr}")

    not_ready = f"readiness marker not published within {import_budget_seconds} seconds"
    not_exited = f"lifecycle did not exit within {lifecycle_budget_seconds} seconds"
    try:
        while True:
            probe_at = clock()
            if not ready_marker.exists():
                last_absent_at = probe_at
                if exited:
                    _fail("lifecycle probe exited before readiness")
                if probe_at >= import_deadline:
                    _fail(not_ready)
                try:
                    stdout, stderr = child.communicate(
                        timeout=min(_READY_POLL_SECONDS, import_deadline - probe_at)
                    )
                except subprocess.TimeoutExpired:
                    continue
                exited = True
                continue
            observed_at = clock()
            if observed_at > import_deadline:
                _fail(not_ready)
            try:
                content: str | None = ready_marker.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = None
            if content != f"ready {child.pid}\n":
                _fail(f"invalid readiness marker: {content!r}")
            break
        lifecycle_deadline = last_absent_at + lifecycle_budget_seconds
        if not exited:
            try:
                stdout, stderr = child.communicate(timeout=max(0.0, lifecycle_deadline - clock()))
            except subprocess.TimeoutExpired:
                _fail(not_exited)
            exited = True
        if clock() > lifecycle_deadline:
            _fail(not_exited)
    except BaseException as error:
        if not cleaned:
            _cleanup_once()
            for cleanup_error in cleanup_errors:
                error.add_note(f"lifecycle probe cleanup error: {cleanup_error!r}")
        raise
    return child, stdout, stderr


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_timeout_cleanup_kills_independent_mcp_group(tmp_path: Path) -> None:
    """Timeout cleanup kills both independently-sessioned process groups."""
    mcp_pid_file = tmp_path / "nested-child.pid"
    topology_probe = textwrap.dedent(
        """
        import subprocess
        import sys
        import time
        from pathlib import Path

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        Path(sys.argv[1]).write_text(str(child.pid))
        time.sleep(30)
        """
    )
    outer = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", topology_probe, str(mcp_pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cleanup_sent = False
    try:
        deadline = time.monotonic() + 2.0
        while not mcp_pid_file.exists():
            if time.monotonic() >= deadline:
                pytest.fail("topology probe did not publish its nested child pid")
            time.sleep(0.01)
        _kill_probe_process_groups(outer.pid, mcp_pid_file)
        cleanup_sent = True
        outer.communicate(timeout=5.0)
        assert outer.returncode == -signal.SIGKILL
    finally:
        if not cleanup_sent:
            _kill_probe_process_groups(outer.pid, mcp_pid_file)
        with contextlib.suppress(subprocess.TimeoutExpired):
            outer.communicate(timeout=1.0)


@pytest.mark.skipif(
    not hasattr(os, "killpg") or not os.path.isfile(resolve_mcp_command(DEFAULT_MCP_COMMAND)),
    reason="requires POSIX process groups and the real mock-driver MCP executable",
)
def test_parse_and_real_mcp_subprocess_exits_cleanly(tmp_path: Path) -> None:
    """Parse work plus a clean real-MCP stop must not hold process pipes open."""
    mcp_pid_file = tmp_path / "mcp-child.pid"
    probe = textwrap.dedent(
        """
        import asyncio
        import os
        import sys
        from pathlib import Path

        from roastpilot_agent import bean_sourcing
        from roastpilot_agent.mcp_client import MCPServerProcess, RoasterMCPClient


        class PidCapturingProcess(MCPServerProcess):
            def __init__(self) -> None:
                super().__init__()
                self.child_pid = None

            def _register_force_terminate(self, pid: int) -> None:
                self.child_pid = pid
                Path(sys.argv[1]).write_text(str(pid))
                super()._register_force_terminate(pid)


        async def main() -> None:
            process = PidCapturingProcess()
            await process.start()
            try:
                info = await RoasterMCPClient(process.call_tool).get_server_info()
                assert info.bootstrap_safe is True
                markdown = await bean_sourcing._extract_page_markdown_bounded(
                    "<html><body><main>Coffee details and tasting notes.</main></body></html>",
                    timeout_seconds=5.0,
                )
                assert markdown is not None
            finally:
                await process.stop()

            assert process.child_pid is not None
            try:
                os.kill(process.child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"MCP child pid {process.child_pid} survived clean stop")


        def publish_ready() -> None:
            marker = Path(sys.argv[2])
            staging = marker.with_name(marker.name + ".tmp")
            staging.write_text(f"ready {os.getpid()}\\n")
            os.replace(staging, marker)


        publish_ready()
        asyncio.run(main())
        """
    )
    ready_marker = tmp_path / "lifecycle-ready.marker"
    child, stdout, stderr = _run_phased_lifecycle_probe(
        [sys.executable, "-c", probe, str(mcp_pid_file), str(ready_marker)],
        ready_marker=ready_marker,
        mcp_pid_file=mcp_pid_file,
    )
    if child.returncode != 0:
        _kill_captured_process_group(mcp_pid_file)
    assert child.returncode == 0, (
        f"lifecycle probe exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_live_exit_watchdog_bounds_cancellation_resistant_owner(tmp_path: Path) -> None:
    """Ordered teardown is durable before a stuck owner forces exit 70."""
    evidence_file = tmp_path / "teardown-evidence.txt"
    child_pid_file = tmp_path / "mcp-child.pid"
    probe = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import subprocess
        import sys
        from pathlib import Path

        from roastpilot_agent.cli import _LiveExitGuard, _finish_live_teardown

        evidence = Path(sys.argv[1])
        pid_file = Path(sys.argv[2])
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        pid_file.write_text(str(child.pid))

        def record(value: str) -> None:
            with evidence.open("a", encoding="utf-8") as stream:
                stream.write(value + "\\n")
                stream.flush()
                os.fsync(stream.fileno())

        class Service:
            async def safe_shutdown_heat_off(self) -> None:
                record("heat-off")

            async def shutdown(self) -> None:
                record("service-stop")

            async def record_child_stop_unconfirmed(self, *, stop_unconfirmed: bool) -> None:
                record(f"unconfirmed:{stop_unconfirmed}")

        class MCP:
            stop_unconfirmed = True

            async def stop(self) -> None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
                record("mcp-stop")

        class Store:
            async def close(self) -> None:
                record("store-close")

        async def cancellation_resistant_owner() -> None:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass

        guard = _LiveExitGuard(grace_seconds=0.25)

        async def main() -> None:
            asyncio.create_task(cancellation_resistant_owner(), name="retained-mcp-owner")
            await _finish_live_teardown(Service(), MCP(), Store(), guard)

        asyncio.run(main())
        guard.disarm()
        """
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", probe, str(evidence_file), str(child_pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        _kill_probe_process_groups(process.pid, child_pid_file)
        stdout, stderr = process.communicate(timeout=2.0)
        pytest.fail(f"live-exit probe exceeded bound\nstdout:\n{stdout}\nstderr:\n{stderr}")

    assert process.returncode == 70, stderr
    assert evidence_file.read_text(encoding="utf-8").splitlines() == [
        "heat-off",
        "service-stop",
        "mcp-stop",
        "unconfirmed:True",
        "store-close",
    ]
    assert "retained-mcp-owner" in stderr
    child_pid = int(child_pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_live_exit_watchdog_bounds_startup_failure_owner(tmp_path: Path) -> None:
    """Startup cleanup arms the watchdog even before a service is returned."""
    evidence_file = tmp_path / "startup-cleanup.txt"
    probe = textwrap.dedent(
        """
        import asyncio
        import sys
        from pathlib import Path

        from roastpilot_agent import cli, config_store, live
        from roastpilot_agent.config import AppConfig
        from roastpilot_agent.mcp_client import MCPConnectionError

        evidence = Path(sys.argv[1])

        async def cancellation_resistant_owner() -> None:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass

        async def fail_after_cleanup(_config: object, *, store_path: object) -> object:
            asyncio.create_task(
                cancellation_resistant_owner(),
                name="retained-startup-mcp-owner",
            )
            evidence.write_text("startup-cleanup-complete", encoding="utf-8")
            raise MCPConnectionError("startup failed after bounded cleanup")

        config_store.load_app_config = lambda: (AppConfig(), set())
        live.forward_coffee_env = lambda _config: None
        live.build_live_service = fail_after_cleanup
        args = cli._build_parser().parse_args(["serve", "--port", "0"])
        guard = cli._LiveExitGuard(grace_seconds=0.25)
        signal_guard = cli._LiveSignalGuard()
        asyncio.run(cli._serve_live(args, exit_guard=guard, signal_guard=signal_guard))
        guard.disarm()
        """
    )
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe, str(evidence_file)],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert process.returncode == 70, process.stderr
    assert evidence_file.read_text(encoding="utf-8") == "startup-cleanup-complete"
    assert "retained-startup-mcp-owner" in process.stderr


def test_signal_during_asyncio_run_finalization_is_propagated(tmp_path: Path) -> None:
    """A late SIGTERM cannot be swallowed after the live coroutine returns."""
    finalizing_file = tmp_path / "asyncio-finalizing.txt"
    probe = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import sys
        import threading
        import time
        from pathlib import Path

        from roastpilot_agent import cli

        finalizing = Path(sys.argv[1])

        async def residual_task() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                finalizing.write_text("cancelling", encoding="utf-8")
                await asyncio.sleep(0.35)

        async def fake_serve(
            _args: object,
            *,
            exit_guard: object,
            signal_guard: cli._LiveSignalGuard,
        ) -> int:
            del exit_guard
            signal_guard.bind_graceful_handler(lambda _signum, _frame: None)
            asyncio.create_task(residual_task(), name="late-finalization-task")
            return 0

        def send_during_finalization() -> None:
            deadline = time.monotonic() + 3.0
            while not finalizing.exists():
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.01)
            signal.raise_signal(signal.SIGTERM)

        cli._serve_live = fake_serve
        threading.Thread(target=send_during_finalization, daemon=True).start()
        sys.argv = ["roastpilot-agent", "serve", "--port", "0"]
        raise SystemExit(cli.main())
        """
    )
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe, str(finalizing_file)],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert finalizing_file.read_text(encoding="utf-8") == "cancelling"
    assert process.returncode == 128 + signal.SIGTERM, process.stderr


def test_second_sigint_forces_uncertain_exit() -> None:
    """The first SIGINT delegates gracefully; the second exits 70."""
    probe = textwrap.dedent(
        """
        import os
        import signal

        from roastpilot_agent.cli import _LiveSignalGuard

        guard = _LiveSignalGuard()
        guard.bind_graceful_handler(lambda _signum, _frame: os.write(1, b"first\\n"))
        with guard:
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        """
    )
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    assert process.returncode == 70
    assert process.stdout == "first\n"
    assert "hardware state is uncertain" in process.stderr


_SENTINEL = textwrap.dedent(
    """
    import os
    import sys
    import time
    from pathlib import Path

    mode, work, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])


    def publish(content: str) -> None:
        staging = marker.with_name(marker.name + ".tmp")
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, marker)


    def await_go() -> None:
        deadline = time.monotonic() + 10.0
        while not (work / "go").exists():
            if time.monotonic() >= deadline:
                sys.exit(3)
            time.sleep(0.01)


    if mode == "gated-ready-exit":
        await_go()
    if mode in {"silent-owner", "ready-owner"}:
        (work / "started").write_text("started", encoding="utf-8")
    if mode in {"ready-owner", "ready-exit", "gated-ready-exit"}:
        publish(f"ready {os.getpid()}\\n")
    if mode in {"bad-marker", "bad-marker-exit"}:
        publish("ready 0\\n")
    if mode == "bad-marker-exit":
        await_go()
        print("sentinel-stdout-token")
        print("sentinel-stderr-token", file=sys.stderr)
    if mode in {"silent-owner", "ready-owner", "bad-marker"}:
        time.sleep(10.0)
        (work / "self-exited").write_text("self-exited", encoding="utf-8")
    """
)

_requires_killpg = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="requires POSIX process groups"
)


class _ProofHarness:
    """Owned handles, a Popen spy and a guarded ``killpg`` for phased-probe proofs."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.ready_marker = tmp_path / "ready.marker"
        self.mcp_pid_file = tmp_path / "mcp-child.pid"
        self.real_killpg = os.killpg
        self.instances: list[subprocess.Popen[str]] = []
        self.signals: list[tuple[int, int]] = []
        self.communicate_calls: list[tuple[float | None, bool]] = []
        self.force_post_kill_timeout = False
        self.release_file: Path | None = None
        self.sleeper: subprocess.Popen[bytes] | None = None
        self.rescued: list[int] = []

    def arm(self, *, sleeper: bool = True) -> None:
        """Spawn the simulated MCP group (optional), then install the spy and guard."""
        if sleeper:
            self.sleeper = subprocess.Popen(  # noqa: S603
                [sys.executable, "-c", "import time; time.sleep(20)"],
                start_new_session=True,
            )
            self.mcp_pid_file.write_text(str(self.sleeper.pid))
        harness = self

        class _CapturingPopen(subprocess.Popen[str]):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                harness.instances.append(self)

            def communicate(
                self, input: str | None = None, timeout: float | None = None
            ) -> tuple[Any, Any]:
                post_kill = bool(harness.signals)
                harness.communicate_calls.append((timeout, post_kill))
                if post_kill and harness.force_post_kill_timeout:
                    raise subprocess.TimeoutExpired(self.args, timeout or 0.0)
                release = harness.release_file
                if release is not None and not release.exists():
                    release.write_text("go", encoding="utf-8")
                    self.wait(timeout=5.0)
                return super().communicate(input, timeout)

        self.monkeypatch.setattr(subprocess, "Popen", _CapturingPopen)
        self.monkeypatch.setattr(os, "killpg", self._guarded_killpg)

    def _guarded_killpg(self, pgid: int, sig: int) -> None:
        self.signals.append((pgid, sig))
        if any(h.pid == pgid and h.returncode is not None for h in self.instances):
            raise AssertionError("signal to retired outer")
        self.real_killpg(pgid, sig)

    def run(
        self,
        mode: str,
        *,
        import_budget_seconds: float = _IMPORT_READY_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[subprocess.Popen[str], str, str]:
        """Run the helper under proof against one sentinel mode."""
        return _run_phased_lifecycle_probe(
            [sys.executable, "-c", _SENTINEL, mode, str(self.tmp_path), str(self.ready_marker)],
            ready_marker=self.ready_marker,
            mcp_pid_file=self.mcp_pid_file,
            import_budget_seconds=import_budget_seconds,
            clock=clock,
        )

    def outer(self) -> subprocess.Popen[str]:
        """Return the single captured outer handle."""
        assert len(self.instances) == 1
        return self.instances[0]

    def signalled(self, pid: int) -> int:
        """Count guarded signal records for one process group."""
        return [pgid for pgid, _ in self.signals].count(pid)

    def assert_sleeper_killed(self) -> None:
        """The simulated MCP group was SIGKILLed by the helper, not left running."""
        assert self.sleeper is not None
        assert self.sleeper.wait(timeout=5.0) == -signal.SIGKILL

    def rescue(self) -> None:
        """Independently kill and reap only still-running owned handles."""
        handles: list[subprocess.Popen[Any]] = [*self.instances]
        if self.sleeper is not None:
            handles.append(self.sleeper)
        for handle in handles:
            if handle.poll() is None:
                self.rescued.append(handle.pid)
                with contextlib.suppress(ProcessLookupError):
                    self.real_killpg(handle.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    handle.wait(timeout=5.0)
            for stream in (handle.stdout, handle.stderr):
                if stream is not None:
                    stream.close()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_ProofHarness]:
    """Yield a proof harness whose teardown rescue must never be needed."""
    proof = _ProofHarness(tmp_path, monkeypatch)
    yield proof
    proof.rescue()
    assert not proof.rescued, f"rescue had to kill owned pids {proof.rescued}"


def _offset_clock(trigger: Path, offset: float) -> Callable[[], float]:
    """Real monotonic time plus ``offset`` once ``trigger`` exists."""
    return lambda: time.monotonic() + (offset if trigger.exists() else 0.0)


@_requires_killpg
@pytest.mark.parametrize("clock_fault", [False, True], ids=["deadline", "clock-error"])
def test_phased_probe_readiness_overrun_kills_owned_groups(
    harness: _ProofHarness, clock_fault: bool
) -> None:
    """R1/R1b: a silent child past the import budget, or a clock fault, kills both groups."""
    harness.arm()
    started = harness.tmp_path / "started"
    clock_error = RuntimeError("injected clock failure")
    deadline_clock = _offset_clock(started, 31.0)

    def clock() -> float:
        if clock_fault and started.exists():
            raise clock_error
        return deadline_clock()

    expected: type[BaseException] = RuntimeError if clock_fault else pytest.fail.Exception
    with pytest.raises(expected) as excinfo:
        harness.run("silent-owner", clock=clock)
    if clock_fault:
        assert excinfo.value is clock_error
    else:
        assert "readiness marker not published within" in str(excinfo.value)
    assert harness.outer().returncode == -signal.SIGKILL
    harness.assert_sleeper_killed()
    assert not (harness.tmp_path / "self-exited").exists()


@_requires_killpg
def test_phased_probe_cleanup_error_preserves_primary_and_kills_outer(
    harness: _ProofHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing MCP-group cleanup is noted on the primary error and the outer still dies."""
    harness.arm()
    started = harness.tmp_path / "started"
    clock_error = RuntimeError("injected clock failure")
    genuine = _kill_captured_process_group

    def failing_mcp_kill(pid_file: Path) -> None:
        genuine(pid_file)
        raise RuntimeError("injected cleanup failure")

    def clock() -> float:
        if started.exists():
            raise clock_error
        return time.monotonic()

    monkeypatch.setitem(globals(), "_kill_captured_process_group", failing_mcp_kill)
    with pytest.raises(RuntimeError) as excinfo:
        harness.run("silent-owner", clock=clock)
    assert excinfo.value is clock_error
    assert any("injected cleanup failure" in note for note in excinfo.value.__notes__)
    assert harness.outer().returncode == -signal.SIGKILL
    harness.assert_sleeper_killed()


@_requires_killpg
def test_phased_probe_post_kill_collection_is_single_and_bounded(harness: _ProofHarness) -> None:
    """R-T: one bounded post-kill collection; a stuck child keeps the phase failure visible."""
    harness.arm()
    harness.force_post_kill_timeout = True
    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness.run("silent-owner", clock=_offset_clock(harness.tmp_path / "started", 31.0))
    message = str(excinfo.value)
    assert "did not exit after both groups were killed" in message
    assert "readiness marker not published within" in message
    assert harness.sleeper is not None
    assert harness.signalled(harness.outer().pid) == 1
    assert harness.signalled(harness.sleeper.pid) == 1
    assert [timeout for timeout, post_kill in harness.communicate_calls if post_kill] == [5.0]
    assert harness.outer().wait(timeout=5.0) == -signal.SIGKILL
    harness.assert_sleeper_killed()


@_requires_killpg
def test_phased_probe_lifecycle_overrun_kills_owned_groups(harness: _ProofHarness) -> None:
    """R2: a ready child that outlives the lifecycle budget kills both groups."""
    harness.arm()
    with pytest.raises(pytest.fail.Exception, match="lifecycle did not exit within"):
        harness.run(
            "ready-owner",
            import_budget_seconds=40.0,
            clock=_offset_clock(harness.ready_marker, 35.0),
        )
    assert harness.outer().returncode == -signal.SIGKILL
    harness.assert_sleeper_killed()
    assert not (harness.tmp_path / "self-exited").exists()


@_requires_killpg
def test_phased_probe_exit_before_readiness_never_signals_retired_outer(
    harness: _ProofHarness,
) -> None:
    """R3: premature exit fails, cleans the MCP group, and never signals the reaped outer."""
    harness.arm()
    with pytest.raises(pytest.fail.Exception, match="exited before readiness"):
        harness.run("exit")
    outer = harness.outer()
    assert outer.returncode == 0
    assert harness.signalled(outer.pid) == 0
    harness.assert_sleeper_killed()


@_requires_killpg
def test_phased_probe_rejects_invalid_marker(harness: _ProofHarness) -> None:
    """R4: a marker not bound to the child's PID is a hard failure."""
    harness.arm()
    with pytest.raises(pytest.fail.Exception, match="invalid readiness marker"):
        harness.run("bad-marker")
    assert harness.outer().returncode == -signal.SIGKILL
    harness.assert_sleeper_killed()
    assert not (harness.tmp_path / "self-exited").exists()


@_requires_killpg
def test_phased_probe_collects_reaped_owner_output_once(harness: _ProofHarness) -> None:
    """A reaped but uncollected owner is never signalled; its output is still collected."""
    harness.arm()
    go = harness.tmp_path / "go"

    def clock() -> float:
        if harness.ready_marker.exists():
            go.touch()
            harness.outer().wait(timeout=5.0)
        return time.monotonic()

    with pytest.raises(pytest.fail.Exception, match="invalid readiness marker") as excinfo:
        harness.run("bad-marker-exit", clock=clock)
    message = str(excinfo.value)
    assert "sentinel-stdout-token" in message
    assert "sentinel-stderr-token" in message
    outer = harness.outer()
    assert outer.returncode == 0
    assert harness.signalled(outer.pid) == 0
    assert outer.stdout is not None and outer.stdout.closed
    assert outer.stderr is not None and outer.stderr.closed
    harness.assert_sleeper_killed()


@_requires_killpg
@pytest.mark.parametrize("stale_name", ["ready.marker", "ready.marker.tmp"])
def test_phased_probe_rejects_stale_marker_before_spawn(
    harness: _ProofHarness, stale_name: str
) -> None:
    """R5: a pre-existing marker or staging file fails before anything is spawned."""
    harness.arm(sleeper=False)
    (harness.tmp_path / stale_name).write_text("ready 0\n")
    with pytest.raises(pytest.fail.Exception, match="stale readiness marker"):
        harness.run("exit")
    assert harness.instances == []


@_requires_killpg
@pytest.mark.parametrize("gated", [False, True], ids=["any-order", "exited-before-observation"])
def test_phased_probe_accepts_valid_ready_then_exit(harness: _ProofHarness, gated: bool) -> None:
    """R6: a valid marker from a child that has already exited is success, not premature."""
    harness.arm(sleeper=False)
    if gated:
        harness.release_file = harness.tmp_path / "go"
    child, stdout, stderr = harness.run("gated-ready-exit" if gated else "ready-exit")
    assert child is harness.outer()
    assert child.returncode == 0
    assert (stdout, stderr) == ("", "")
    if gated:
        assert harness.communicate_calls == [(_READY_POLL_SECONDS, False)]


@_requires_killpg
def test_phased_probe_rejects_late_readiness_observation(harness: _ProofHarness) -> None:
    """D1: a marker first observed after the import deadline is never accepted."""
    harness.arm()
    with pytest.raises(pytest.fail.Exception, match="readiness marker not published within"):
        harness.run("ready-exit", clock=_offset_clock(harness.ready_marker, 31.0))
    harness.assert_sleeper_killed()


@_requires_killpg
def test_phased_probe_rejects_late_completion_observation(harness: _ProofHarness) -> None:
    """D2: completion observed past the publication-preceding lifecycle anchor fails."""
    harness.arm()

    def clock() -> float:
        if harness.instances and harness.instances[0].returncode is not None:
            return time.monotonic() + 31.0
        if harness.ready_marker.exists():
            return time.monotonic() + 10.0
        return time.monotonic()

    with pytest.raises(pytest.fail.Exception, match="lifecycle did not exit within"):
        harness.run("ready-exit", import_budget_seconds=40.0, clock=clock)
    harness.assert_sleeper_killed()
