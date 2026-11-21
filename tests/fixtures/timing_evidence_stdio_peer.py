"""Hardware-free timing peer and protected-child instrumentation.

P1 instrumentation counts only the seven mock actuator methods. Lifecycle
connect/disconnect and read_state are deliberately excluded; baselines follow
setup. No Hottop staged command exists on this controlled mock interface.
"""

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import Field

SessionId = Annotated[str, Field(min_length=1, max_length=128)]

ACTUATORS = (
    "set_heat",
    "set_fan",
    "drop_beans",
    "start_cooling",
    "stop_cooling",
    "emergency_stop",
    "contain_heat_fixed_zero",
)


def instrument_p1(root: Path) -> None:
    """Instrument the genuine protected mock driver without changing its source."""
    from importlib.metadata import version

    import coffee_roaster_mcp

    drivers: Any = import_module("coffee_roaster_mcp.drivers")
    server: Any = import_module("coffee_roaster_mcp.mcp_server")

    driver = drivers.MockRoasterDriver()
    driver.connect()
    interface = {
        name
        for name in (*dir(drivers.RoasterDriver), *dir(drivers.FixedZeroContainmentDriver))
        if not name.startswith("_")
    }
    # Staged Hottop-only ports are absent from MockRoasterDriver.
    assert set(ACTUATORS) <= interface
    mock_methods = {
        name for name in dir(driver) if not name.startswith("_") and callable(getattr(driver, name))
    }
    assert mock_methods - {"connect", "disconnect", "read_state", "read_lifecycle_evidence"} == set(
        ACTUATORS
    )
    counts: dict[str, int] = dict.fromkeys(ACTUATORS, 0)

    def wrap(name: str) -> None:
        original = getattr(driver, name)

        def counted(*args: object, **kwargs: object) -> object:
            counts[name] += 1
            (root / "actuators.json").write_text(json.dumps(counts))
            return original(*args, **kwargs)

        setattr(driver, name, counted)

    for name in ACTUATORS:
        wrap(name)
    (root / "actuators.json").write_text(json.dumps(counts))
    before = counts["set_heat"]
    driver.set_heat(heat_level_percent=0)
    assert counts["set_heat"] == before + 1
    from threading import local

    reading = local()
    original_evidence = server._run_timing_evidence

    def evidence_read(*args: object, **kwargs: object) -> Any:
        reading.active = True
        try:
            return original_evidence(*args, **kwargs)
        finally:
            reading.active = False

    server._run_timing_evidence = evidence_read
    original_read = driver.read_state

    def read_state() -> object:
        switch = root / "fail-next-evidence"
        if getattr(reading, "active", False) and switch.exists():
            switch.unlink()
            raise RuntimeError("fixture-only-read-failure")
        return original_read()

    driver.read_state = read_state

    def create_driver(*args: object, **kwargs: object) -> Any:
        return driver

    server.create_roaster_driver = create_driver
    (root / "identity.json").write_text(
        json.dumps(
            {
                "package": coffee_roaster_mcp.__file__,
                "server": server.__file__,
                "drivers": drivers.__file__,
                "versions": {name: version(name) for name in ("mcp", "pydantic", "pydantic-core")},
                "wrapped": list(ACTUATORS),
                "self_test": counts["set_heat"],
            }
        )
    )


def run_peer() -> None:
    """Run a deliberately incompatible-output-schema strict-wire witness."""
    mode, root = sys.argv[1], Path(sys.argv[2])

    def log(name: str) -> None:
        with (root / "calls").open("a") as file:
            file.write(name + "\n")

    class Peer(FastMCP):
        """Advertise exact input and an output schema raw reads must ignore."""

        async def list_tools(self) -> list[Tool]:
            """Record discovery and close the advertised input schema."""
            log("list")
            tools = await super().list_tools()
            for tool in tools:
                if tool.name == "get_timing_evidence":
                    tool.inputSchema["additionalProperties"] = False
                    tool.outputSchema = {"not": {}}
            return tools

    peer = Peer("timing-evidence-witness")

    @peer.tool()
    def get_server_info() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Supply the health response."""
        return {"ok": True}

    @peer.tool()
    def start_roast_session() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Supply a fixture session identity."""
        log("start")
        return cast("dict[str, object]", json.loads((root / "start.json").read_text()))

    @peer.tool()
    def get_timing_evidence(  # pyright: ignore[reportUnusedFunction]
        expected_session_id: SessionId, evidence_protocol: Literal["timing_evidence/v1"]
    ) -> CallToolResult:
        """Replay exact JSON primitives without output schema adaptation."""
        log("get_timing_evidence")
        payload = json.loads((root / "response.json").read_text())
        if mode == "malformed-error":
            return CallToolResult.model_construct(content=[], isError="false")
        if mode == "scalar":
            return CallToolResult.model_construct(content=[], structuredContent=42)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structuredContent=None if mode == "text-only" else payload,
            isError=mode == "error",
        )

    peer.run()


if __name__ == "__main__":
    run_peer()
