"""Capture per-tool contract fixtures from the real coffee-roaster-mcp (E5-S3).

Runs the agent's own E5-S2 transport (MCPServerProcess → RoasterMCPClient)
against the real server in its bootstrap-safe mock mode and saves every
tool's raw result payload under tests/fixtures/mcp-tool-results/. Re-run
on coffee-roaster-mcp dependency bumps alongside the mcp-contract-checker
sub-agent.

Usage:
    python scripts/capture_mcp_fixtures.py [path-to-coffee-roaster-mcp-binary]

The binary defaults to whatever `coffee-roaster-mcp` resolves to on PATH;
pass a scratch-venv binary explicitly to keep the project venv clean.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roastpilot_agent.config import MCPConfig  # noqa: E402
from roastpilot_agent.mcp_client import MCPServerProcess  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mcp-tool-results"


async def capture(command: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = MCPConfig(command=command, call_timeout_seconds=10.0)

    def save(tool: str, payload: object) -> None:
        path = OUT_DIR / f"{tool}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"captured {tool} -> {path.name}")

    # The mock server writes logs into its working directory; run from a
    # temp dir so nothing lands in the repo.
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.chdir(tmp)
        process = MCPServerProcess(config)
        await process.start()
        try:
            # Read-only tools first.
            save("get_server_info", await process.call_tool("get_server_info", {}))
            save("get_runtime_config", await process.call_tool("get_runtime_config", {}))
            # Full mock roast, in command order.
            save("start_roast_session", await process.call_tool("start_roast_session", {}))
            save("set_heat", await process.call_tool("set_heat", {"heat_level_percent": 70}))
            save("set_fan", await process.call_tool("set_fan", {"fan_level_percent": 40}))
            save("mark_beans_added", await process.call_tool("mark_beans_added", {}))
            save("get_roast_state", await process.call_tool("get_roast_state", {}))
            save("mark_first_crack", await process.call_tool("mark_first_crack", {}))
            save("drop_beans", await process.call_tool("drop_beans", {}))
            try:
                save("start_cooling", await process.call_tool("start_cooling", {}))
            except Exception as exc:  # noqa: BLE001 — capture-or-note, never abort
                print(f"start_cooling not capturable in this flow: {exc}")
            save("stop_cooling", await process.call_tool("stop_cooling", {}))
            save("export_roast_log", await process.call_tool("export_roast_log", {}))
            # Emergency stop needs an active session and ends it — capture
            # it in a fresh second session.
            await process.call_tool("start_roast_session", {})
            save(
                "emergency_stop",
                await process.call_tool("emergency_stop", {"reason": "fixture capture"}),
            )
        finally:
            await process.stop()


if __name__ == "__main__":
    binary = sys.argv[1] if len(sys.argv) > 1 else "coffee-roaster-mcp"
    asyncio.run(capture(binary))
