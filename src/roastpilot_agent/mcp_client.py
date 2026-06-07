"""Typed wrapper over the coffee-roaster-mcp stdio server (component plan §2, §4).

Owns the MCP child process (decision D6): spawn, health, restart → recovery.
Wraps exactly the verified 13-tool surface of coffee-roaster-mcp v0.1.3;
Pydantic mirrors of ``RoastSessionState`` / ``T0Status`` / ``FirstCrackStatus``
and contract fixtures land in E5.

Neither the advisor nor the SPA ever sees this client: every write command
arrives via explicit controller methods carrying a SafetyEvaluation.
"""


class RoasterMCPClient:
    """Typed client for the verified 13-tool MCP surface (E5).

    Tools: get_server_info, get_runtime_config, start_roast_session,
    get_roast_state, set_heat, set_fan, mark_beans_added, mark_first_crack,
    drop_beans, start_cooling, stop_cooling, export_roast_log, emergency_stop.
    """

    async def start(self) -> None:
        """Spawn the coffee-roaster-mcp stdio child process (E5)."""
        raise NotImplementedError("E5: MCP child-process lifecycle")

    async def stop(self) -> None:
        """Terminate the MCP child process cleanly (E5)."""
        raise NotImplementedError("E5: MCP child-process lifecycle")
