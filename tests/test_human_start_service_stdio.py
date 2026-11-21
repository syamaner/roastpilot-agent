# pyright: reportPrivateUsage=false
"""Actual SDK wire composition for the private mock Start service.

The in-memory strict JSON-RPC peer is synthetic; no process or hardware is used.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from roastpilot_agent.api import RoastService
from roastpilot_agent.human_bootstrap import BOOTSTRAP_TOOL_INVENTORY
from roastpilot_agent.human_protection import ControlAuthority
from roastpilot_agent.mcp_client import RoasterControlAdapter
from roastpilot_agent.models import RestartClearanceRequest, StartAttemptHoldState
from roastpilot_agent.store import RoastStore
from tests.test_controller import PROFILE
from tests.test_mcp_client import (
    CANNED,
    _BootstrapPeer,
    _bs_process,
    _hs_payload,
    _hs_raw,
)


@asynccontextmanager
async def service_sdk(peer: _BootstrapPeer) -> AsyncGenerator[ClientSession]:
    """Answer actual SDK requests, including its ordinary result-schema discovery."""
    to_client, client_recv = anyio.create_memory_object_stream[SessionMessage | Exception](8)
    to_peer, peer_recv = anyio.create_memory_object_stream[SessionMessage](8)

    async def serve() -> None:
        async for message in peer_recv:
            request = message.message.root
            assert isinstance(request, JSONRPCRequest)
            peer.requests.append((request.method, request.params))
            if request.method == "tools/list":
                result: dict[str, object] = {
                    "tools": [
                        {"name": name, "inputSchema": {"type": "object"}}
                        for name in (*BOOTSTRAP_TOOL_INVENTORY, "get_capture_evidence")
                    ]
                }
            else:
                params = request.params or {}
                name = params["name"]
                assert isinstance(name, str)
                reply = peer.results[name]
                assert reply is not None
                result = reply
            await to_client.send(
                SessionMessage(
                    JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=request.id, result=result))
                )
            )

    async with anyio.create_task_group() as group:
        group.start_soon(serve)
        async with ClientSession(client_recv, to_peer) as session:
            yield session
        group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_actual_sdk_service_claim_and_first_adapter_read(tmp_path: Path) -> None:
    """Use real ClientSession envelopes, strict inventory and real SQLite history."""
    peer = _BootstrapPeer()
    peer.results["start_roast_session"] = _hs_raw(_hs_payload()).model_dump(
        by_alias=True, exclude_none=True
    )
    peer.results["set_recording_metadata"] = _hs_raw(
        {"origin": "ethiopia-harness", "roast_num": 1}
    ).model_dump(by_alias=True, exclude_none=True)
    state = deepcopy(cast("dict[str, object]", CANNED["get_roast_state"]))
    state.update(
        session_id="a" * 32,
        phase="pre_roast",
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
        events=[],
    )
    for key in ("beans_added_at_utc", "first_crack_at_utc", "beans_dropped_at_utc"):
        state[key] = None
    device = cast("dict[str, object]", state["device_state"])
    device.update(
        driver="mock",
        heat_level_percent=0,
        fan_level_percent=0,
        raw_vendor_data={"status_packet_count": 1, "ignored_temperature_packet_count": 0},
    )
    peer.results["get_roast_state"] = _hs_raw(state).model_dump(by_alias=True, exclude_none=True)
    store = RoastStore(tmp_path / "sdk.sqlite3")
    await store.initialize()
    try:
        async with service_sdk(peer) as session:
            process = _bs_process(session)
            service = RoastService(
                store,
                mcp=process,
                authority=ControlAuthority.HUMAN,
                run_loop=False,
                _mock_human_start=True,
            )
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
            peer.requests.clear()
            detail = await service._start_mock_human_roast(PROFILE)
            assert detail.id == service.active_run_id
            assert isinstance(service._roaster, RoasterControlAdapter)
            reading = await service._roaster.read_human_evidence()
            assert reading.bound_child_epoch == process.child_epoch
            assert service._roaster.latest_fault_session_id == "a" * 32
            calls = [
                (params or {}).get("name")
                for method, params in peer.requests
                if method == "tools/call"
            ]
            assert calls == [
                "get_server_info",
                "get_runtime_config",
                "get_capture_evidence",
                "set_recording_metadata",
                "start_roast_session",
                "get_roast_state",
            ]
            assert (await store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
            async with store.connection.execute(
                "SELECT payload_json FROM operator_actions "
                "WHERE action='start_attempt_hold_settled'"
            ) as cursor:
                rows = list(await cursor.fetchall())
            assert len(rows) == 1 and "accepted_session" in rows[0][0]
            await service.shutdown()
    finally:
        await store.close()
