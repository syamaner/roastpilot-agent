"""Fresh-process lifecycle regression tests."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from roastpilot_agent.config import DEFAULT_MCP_COMMAND
from roastpilot_agent.mcp_client import resolve_mcp_command


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


        asyncio.run(main())
        """
    )
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", probe, str(mcp_pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = child.communicate(timeout=30.0)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout = stderr = ""
    finally:
        if timed_out:
            _kill_probe_process_groups(child.pid, mcp_pid_file)

    if timed_out:
        try:
            stdout, stderr = child.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail("timed-out lifecycle probe did not exit after both groups were killed")
        pytest.fail(
            "parse/MCP lifecycle probe did not exit within 30 seconds\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    if child.returncode != 0:
        _kill_captured_process_group(mcp_pid_file)
    assert child.returncode == 0, (
        f"lifecycle probe exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
