"""Hardware-free stdio FastMCP peer for the fixed-zero transport contract."""

import asyncio
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock, Tool
from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

_MODE = sys.argv[1]
_ROOT = Path(sys.argv[2])
_TOOL = "contain_heat_fixed_zero"


def _record(name: str) -> None:
    with (_ROOT / "calls").open("a") as calls:
        calls.write(f"{name}\n")


class WitnessServer(FastMCP):
    """Advertise the precise consumer request schema and record inventory reads."""

    async def list_tools(self) -> list[Tool]:
        """Return one tool inventory."""
        _record("list")
        tools = await super().list_tools()
        for tool in tools:
            if tool.name == _TOOL:
                tool.inputSchema["additionalProperties"] = False
                if _MODE in {"scalar", "null", "missing", "outer-error", "valid"}:
                    # A caller that validates this arbitrary schema cannot
                    # reach the consumer's closed response grammar.
                    tool.outputSchema = {"not": {}}
        return tools


class ExplicitNullReply(CallToolResult):
    """Emit a JSON null even when the SDK excludes ordinary optional fields."""

    @model_serializer(mode="wrap")
    def serialize_with_null(self, serialize: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Preserve the explicitly null structured member on the wire."""
        data = cast("dict[str, object]", serialize(self))
        data["structuredContent"] = None
        return data


server = WitnessServer("fixed-zero-witness")


@server.tool()
def get_server_info() -> dict[str, object]:
    """Answer the transport's legacy health request."""
    return {"ok": True, "driver": "witness"}


@server.tool()
def start_roast_session() -> dict[str, object]:
    """Supply the synthetic Start identity for this child."""
    _record("start")
    payload = json.loads((_ROOT / "start.json").read_text())
    return cast("dict[str, object]", payload)


@server.tool()
def probe() -> dict[str, object]:
    """Mark that the child serviced a second request."""
    _record("probe")
    return {"ok": True, "serviced": True}


def _begin_containment() -> float:
    _record(_TOOL)
    (_ROOT / "entered").touch()
    return time.monotonic() + 20


def _completed_response() -> dict[str, object]:
    (_ROOT / "returned").touch()
    return cast("dict[str, object]", json.loads((_ROOT / "response.json").read_text()))


type_id = Annotated[str, Field(min_length=1, max_length=128)]


def _contain_sync(
    expected_session_id: type_id, containment_protocol: Literal["fixed_zero/v1"]
) -> dict[str, object]:
    """Hold the server event loop until the test releases the operation."""
    deadline = _begin_containment()
    while not (_ROOT / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return _completed_response()


async def _contain_async(
    expected_session_id: type_id, containment_protocol: Literal["fixed_zero/v1"]
) -> dict[str, object]:
    """Hold only this operation until the test releases it."""
    deadline = _begin_containment()
    while not (_ROOT / "release").exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return _completed_response()


server.add_tool(_contain_sync if _MODE == "sync" else _contain_async, name=_TOOL)

if _MODE in {"scalar", "null", "missing", "outer-error", "valid"}:

    @server._mcp_server.call_tool()  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType,reportUntypedFunctionDecorator]
    async def raw_wire_reply(
        name: str, arguments: dict[str, object]
    ) -> Sequence[ContentBlock] | dict[str, object] | CallToolResult:
        """Return a wire result that exercises the consumer's envelope handling."""
        if name != _TOOL:
            return await server.call_tool(name, arguments)
        _record(_TOOL)
        payload = _completed_response()
        if _MODE == "scalar":
            return CallToolResult.model_construct(content=[], structuredContent=42)
        if _MODE == "null":
            return ExplicitNullReply(content=[], structuredContent=None)
        if _MODE == "missing":
            return CallToolResult(content=[])
        return CallToolResult(content=[], structuredContent=payload, isError=_MODE == "outer-error")


server.run()
