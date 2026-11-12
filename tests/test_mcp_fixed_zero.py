"""Hardware-free behavioural contracts for the fixed-zero MCP consumer."""

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from test_mcp_client import SESSION_STATE_PAYLOAD, FakeSession

import roastpilot_agent.mcp_client as adapter_module
from roastpilot_agent.config import MCPConfig
from roastpilot_agent.human_protection import (
    ContainmentOutcome,
    ContainmentReason,
    ContainmentResult,
    CounterClass,
    ToolDiscovery,
)
from roastpilot_agent.mcp_client import (
    InitializableSession,
    MCPConnectionError,
    MCPServerProcess,
    MCPToolError,
    MCPToolTimeoutError,
    RoasterControlAdapter,
    RoasterMCPClient,
)

_TOOL = "contain_heat_fixed_zero"
_PROTOCOL = "fixed_zero/v1"
_SESSION = " e\u0301  "
_STATE: dict[str, object] = {
    "driver": "mock-0",
    "capability_version": 1,
    "heat_level_percent": 0,
    "roast_fan_level_percent": 0,
    "main_fan_level_percent": 100,
    "drum_motor_on": True,
    "cooling_motor_on": False,
    "solenoid_open": True,
}
_ROWS = (
    (
        ContainmentOutcome.REFUSED_IDENTITY,
        "not_invoked",
        None,
        "not_changed",
        False,
        (
            ContainmentReason.UNKNOWN_SESSION,
            ContainmentReason.NOT_LATEST_SESSION,
            ContainmentReason.SESSION_NOT_ACTIVE,
            ContainmentReason.SESSION_FAULTED,
        ),
    ),
    (
        ContainmentOutcome.REFUSED_CAPABILITY,
        "not_invoked",
        _SESSION,
        "not_changed",
        False,
        (
            ContainmentReason.CAPABILITY_ABSENT,
            ContainmentReason.CAPABILITY_VERSION_UNSUPPORTED,
            ContainmentReason.CAPABILITY_MALFORMED,
            ContainmentReason.CAPABILITY_UNREADABLE,
            ContainmentReason.CAPABILITY_PORT_MISSING,
        ),
    ),
    (
        ContainmentOutcome.REFUSED_BUSY,
        "not_invoked",
        _SESSION,
        "not_changed",
        False,
        (ContainmentReason.FINALISATION_IN_PROGRESS, ContainmentReason.COMMAND_IN_PROGRESS),
    ),
    (
        ContainmentOutcome.COMMAND_STATE_RETURNED,
        "invoked",
        _SESSION,
        "recorded_in_memory",
        True,
        (None,),
    ),
    (
        ContainmentOutcome.UNKNOWN_DRIVER_RESULT,
        "invoked",
        _SESSION,
        "unknown",
        False,
        (
            ContainmentReason.DRIVER_RAISED,
            ContainmentReason.DRIVER_RESULT_MISSING,
            ContainmentReason.DRIVER_RESULT_INVALID,
        ),
    ),
    (
        ContainmentOutcome.UNKNOWN_COMPLETION,
        "invoked",
        _SESSION,
        "unknown",
        True,
        (ContainmentReason.COMPLETION_OWNERSHIP_CHANGED, ContainmentReason.PROJECTION_UNCONFIRMED),
    ),
)
_Row = tuple[ContainmentOutcome, str, str | None, str, bool, tuple[ContainmentReason | None, ...]]


def _payload(row: _Row = _ROWS[3], reason: ContainmentReason | None = None) -> dict[str, object]:
    outcome, dispatch, validated, projection, state, reasons = row
    reason = reason or reasons[0]
    return {
        "containment_protocol": _PROTOCOL,
        "containment_version": 1,
        "outcome": outcome.value,
        "request_dispatch": dispatch,
        "automatic_retry_permitted": False,
        "requested_session_id": _SESSION,
        "validated_session_id": validated,
        "commanded_state": _STATE.copy() if state else None,
        "session_projection": projection,
        "durability": "not_provided_by_this_tool",
        "reason_code": None if reason is None else reason.value,
    }


def _result(payload: dict[str, object]) -> CallToolResult:
    return CallToolResult(content=[], structuredContent=payload)


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected_session_id", "containment_protocol"],
        "properties": {
            "expected_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "containment_protocol": {"type": "string", "const": _PROTOCOL},
        },
    }


def _tool(schema: dict[str, object] | None = None) -> Tool:
    return Tool(name=_TOOL, inputSchema=_schema() if schema is None else schema)


class _Session(FakeSession):
    def __init__(
        self, result: object, inventory: object = None, *, session_id: str = _SESSION
    ) -> None:
        super().__init__(result)
        self.inventory = ListToolsResult(tools=[_tool()]) if inventory is None else inventory
        self.session_id = session_id
        self.list_calls = 0

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        if name == "start_roast_session":
            self.calls.append((name, arguments))
            return _result({"session": {**SESSION_STATE_PAYLOAD, "session_id": self.session_id}})
        return await super().call_tool(name, arguments)

    async def list_tools(self) -> object:
        self.list_calls += 1
        if isinstance(self.inventory, Exception):
            raise self.inventory
        return self.inventory


class _LifecycleSpy(MCPServerProcess):
    def __init__(self, session: _Session) -> None:
        super().__init__(session=session)
        self.lifecycle: list[str] = []

    async def start(self) -> None:
        self.lifecycle.append("start")

    async def stop(self) -> None:
        self.lifecycle.append("stop")

    def replace(self, session: _Session | None) -> None:
        self._replace_session(session)  # pyright: ignore[reportPrivateUsage]


async def _prepared(
    result: object, *, session_id: str = _SESSION
) -> tuple[RoasterControlAdapter, _LifecycleSpy, _Session]:
    session = _Session(result, session_id=session_id)
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    await adapter.start_session()
    assert session.calls == [("start_roast_session", {})]
    session.calls.clear()
    return adapter, process, session


@pytest.mark.asyncio
async def test_request_exact_two_keys_no_heat() -> None:
    adapter, process, session = await _prepared(_result(_payload()))
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.COMMAND_STATE_RETURNED
    assert session.calls == [
        (
            _TOOL,
            {
                "expected_session_id": _SESSION,
                "containment_protocol": _PROTOCOL,
            },
        )
    ]
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["", "é" * 129, 16, None])
async def test_invalid_session_identity_never_dispatches(
    monkeypatch: pytest.MonkeyPatch, session_id: object
) -> None:
    adapter, _, session = await _prepared(_result(_payload()))
    monkeypatch.setattr(adapter, "_started_session_id", session_id)
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.NOT_ATTEMPTED
    )
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["é", "é" * 128])
async def test_session_identity_limits_use_exact_code_points(identity: str) -> None:
    payload = {**_payload(), "requested_session_id": identity, "validated_session_id": identity}
    adapter, _, session = await _prepared(_result(payload), session_id=identity)
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.COMMAND_STATE_RETURNED
    assert session.calls[0][1] == {
        "expected_session_id": identity,
        "containment_protocol": _PROTOCOL,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "reason"),
    [
        pytest.param(row, reason, id=f"{row[0].value}-{reason.value if reason else 'no-reason'}")
        for row in _ROWS
        for reason in row[-1]
    ],
)
async def test_valid_rows_and_no_retry(row: _Row, reason: ContainmentReason | None) -> None:
    adapter, process, session = await _prepared(_result(_payload(row, reason)))
    outcome, _, _, _, has_state, _ = row
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        outcome,
        reason,
        True if has_state else None,
        outcome is ContainmentOutcome.UNKNOWN_COMPLETION,
    )
    assert len(session.calls) == 1
    assert process.lifecycle == []


def _row_deviations(row: _Row) -> list[tuple[str, object]]:
    _, dispatch, validated, projection, state, reasons = row
    other_reason = next(reason for reason in ContainmentReason if reason not in reasons)
    deviations: list[tuple[str, object]] = [
        ("request_dispatch", "invoked" if dispatch == "not_invoked" else "not_invoked"),
        ("validated_session_id", _SESSION if validated is None else None),
        ("commanded_state", None if state else _STATE),
        ("reason_code", other_reason.value),
    ]
    deviations.extend(
        ("session_projection", candidate)
        for candidate in ("not_changed", "recorded_in_memory", "unknown")
        if candidate != projection
    )
    if None not in reasons:
        deviations.append(("reason_code", None))
    return deviations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "key", "value"),
    [
        pytest.param(row, key, value, id=f"{row[0].value}-{key}-{index}")
        for row in _ROWS
        for index, (key, value) in enumerate(_row_deviations(row))
    ],
)
async def test_single_row_cell_deviation_is_malformed(row: _Row, key: str, value: object) -> None:
    adapter, process, session = await _prepared(_result({**_payload(row), key: value}))
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    assert len(session.calls) == 1
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["requested_session_id", "validated_session_id"])
async def test_both_identity_echoes_must_match_exactly(key: str) -> None:
    payload = _payload()
    # Also rejects normalisation-equivalent strings.
    payload[key] = " é  "
    adapter, _, _ = await _prepared(_result(payload))
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("layer", "key", "value"),
    [
        ("top", "containment_protocol", 1),
        ("top", "containment_protocol", "fixed_zero/v2"),
        ("top", "containment_version", True),
        ("top", "containment_version", 1.0),
        ("top", "containment_version", 2),
        ("top", "automatic_retry_permitted", "false"),
        ("top", "automatic_retry_permitted", 0),
        ("top", "automatic_retry_permitted", True),
        ("top", "outcome", ["command_state_returned"]),
        ("top", "outcome", "not_attempted"),
        ("top", "durability", "durable"),
        ("top", "extra", "unknown"),
        ("top", "durability", ...),
        ("state", "driver", "Mock"),
        ("state", "driver", "møck"),
        ("state", "driver", 7),
        ("state", "driver", "m" * 65),
        ("state", "capability_version", True),
        ("state", "capability_version", 2),
        ("state", "heat_level_percent", 0.0),
        ("state", "heat_level_percent", 1),
        ("state", "roast_fan_level_percent", True),
        ("state", "roast_fan_level_percent", 101),
        ("state", "main_fan_level_percent", -1),
        ("state", "drum_motor_on", 1),
        ("state", "cooling_motor_on", 0),
        ("state", "solenoid_open", "true"),
        ("state", "extra", 0),
        ("state", "driver", ...),
        ("top", "commanded_state", list(_STATE)),
    ],
)
async def test_exact_wire_types_and_keys(layer: str, key: str, value: object) -> None:
    payload = _payload()
    fields = payload if layer == "top" else cast("dict[str, object]", payload["commanded_state"])
    if value is ...:
        del fields[key]
    else:
        fields[key] = value
    adapter, _, _ = await _prepared(_result(payload))
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        MCPToolTimeoutError("local timeout"),
        MCPConnectionError("pipe"),
        MCPToolError("SDK protocol error"),
        TimeoutError("deadline"),
        RuntimeError("SDK failure"),
    ],
)
async def test_transport_errors_do_not_retry_or_replace(failure: Exception) -> None:
    adapter, process, session = await _prepared(failure)
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.UNKNOWN_TRANSPORT
    )
    assert len(session.calls) == 1
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_cancel_records_transport_unknown_and_propagates() -> None:
    adapter, process, _ = await _prepared(_result(_payload()))
    entered = asyncio.Event()

    class BlockingSession(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name != _TOOL:
                return await super().call_tool(name, arguments)
            self.calls.append((name, arguments))
            entered.set()
            await asyncio.Event().wait()

    session = BlockingSession(None)
    process.replace(session)
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    await adapter.start_session()
    session.calls.clear()
    attempt = asyncio.create_task(adapter.contain_heat_fixed_zero())
    await asyncio.wait_for(entered.wait(), timeout=2)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert adapter.last_containment_result == ContainmentResult(
        ContainmentOutcome.UNKNOWN_TRANSPORT
    )
    assert len(session.calls) == 1
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize("structure", [None, 42, "text", [0], {"result": 42}, ...])
@pytest.mark.parametrize("valid_text", [True, False])
async def test_containment_rejects_text_fallback_and_scalar_evidence(
    monkeypatch: pytest.MonkeyPatch, structure: object, valid_text: bool
) -> None:
    text = json.dumps(_payload()) if valid_text else "{unparseable text"

    def forbid_parser(_result: object) -> object:
        pytest.fail("legacy parser must not see containment evidence")

    # model_construct keeps malformed values for the test seam. Real stdio
    # uses the SDK Result subtype with an uncoerced structuredContent field.
    content = [TextContent(type="text", text=text)]
    result = (
        CallToolResult.model_construct(content=content)
        if structure is ...
        else CallToolResult.model_construct(content=content, structuredContent=structure)
    )
    adapter, process, session = await _prepared(result)
    monkeypatch.setattr(adapter_module, "parse_tool_result", forbid_parser)
    assert (
        await adapter.contain_heat_fixed_zero()
    ).outcome is ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    assert len(session.calls) == 1
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [_payload(), CallToolResult(content=[], structuredContent=_payload(), isError=True)]
)
async def test_sdk_envelope_is_required_and_error_wins(raw: object) -> None:
    adapter, process, session = await _prepared(raw)
    expected = (
        ContainmentOutcome.UNKNOWN_TRANSPORT
        if isinstance(raw, CallToolResult)
        else ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    )
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(expected)
    assert len(session.calls) == 1
    assert process.lifecycle == []


def _changed_schema(key: str, value: object, property_name: str | None = None) -> Tool:
    schema = _schema()
    fields = schema
    if property_name is not None:
        properties = cast("dict[str, dict[str, object]]", schema["properties"])
        fields = properties[property_name]
    if value is ...:
        del fields[key]
    else:
        fields[key] = value
    return _tool(schema)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inventory",
    [
        ListToolsResult(tools=[]),
        ListToolsResult(tools=[_tool(), _tool()]),
        ListToolsResult(tools=[_tool()], nextCursor=""),
        ListToolsResult(tools=[_tool()], nextCursor="next"),
        ListToolsResult(tools=[_changed_schema("additionalProperties", ...)]),
        ListToolsResult(tools=[_changed_schema("additionalProperties", 0)]),
        ListToolsResult(tools=[_changed_schema("type", "array")]),
        ListToolsResult(tools=[_changed_schema("required", ["expected_session_id"] * 2)]),
        ListToolsResult(
            tools=[
                _changed_schema(
                    "required",
                    ["expected_session_id", "containment_protocol", "expected_session_id"],
                )
            ]
        ),
        ListToolsResult(tools=[_changed_schema("properties", {"extra": {}})]),
        ListToolsResult(tools=[_changed_schema("extra", "unexpected")]),
        ListToolsResult(tools=[_changed_schema("const", "fixed_zero/v2", "containment_protocol")]),
        ListToolsResult(tools=[_changed_schema("extra", "unexpected", "containment_protocol")]),
        ListToolsResult(tools=[_changed_schema("extra", "unexpected", "expected_session_id")]),
        *[
            ListToolsResult(tools=[_changed_schema("minLength", bad, "expected_session_id")])
            for bad in [True, 1.0, "1", 2]
        ],
        *[
            ListToolsResult(tools=[_changed_schema("maxLength", bad, "expected_session_id")])
            for bad in [True, 128.0, "128", 129]
        ],
        RuntimeError("list failed"),
    ],
)
async def test_discovery_rejects_nonterminal_listing_and_coerced_bounds(inventory: object) -> None:
    session = _Session(None, inventory)
    process = _LifecycleSpy(session)
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    assert session.list_calls == 1
    assert session.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_human_discovery_is_read_only_pre_start() -> None:
    schema = _schema()
    schema["required"] = ["containment_protocol", "expected_session_id"]
    schema["title"] = "annotation"
    identity = cast("dict[str, dict[str, object]]", schema["properties"])["expected_session_id"]
    identity["description"] = "annotation"
    session = _Session(None, ListToolsResult(tools=[_tool(schema)], nextCursor=None))
    process = _LifecycleSpy(session)
    assert process.fixed_zero_discovery is ToolDiscovery.ABSENT
    assert session.list_calls == 0
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    assert session.list_calls == 1
    assert session.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_discovery_without_a_session_or_listing_capability_is_absent() -> None:
    assert await MCPServerProcess().discover_fixed_zero() is ToolDiscovery.ABSENT
    no_listing = FakeSession(None)
    assert await MCPServerProcess(session=no_listing).discover_fixed_zero() is ToolDiscovery.ABSENT
    assert no_listing.calls == []


@pytest.mark.asyncio
async def test_replacement_invalidates_discovery_and_prior_start_identity() -> None:
    adapter, process, old = await _prepared(_result(_payload()))
    new = _Session(_result(_payload()))
    process.replace(new)
    assert process.fixed_zero_discovery is ToolDiscovery.ABSENT
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert (old.list_calls, new.list_calls) == (1, 1)
    assert old.calls == new.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_late_discovery_cannot_populate_a_replaced_generation() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowInventory(_Session):
        async def list_tools(self) -> object:
            entered.set()
            await release.wait()
            return await super().list_tools()

    old = SlowInventory(None)
    process = _LifecycleSpy(old)
    lookup = asyncio.create_task(process.discover_fixed_zero())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    new = _Session(None, ListToolsResult(tools=[]))
    process.replace(new)
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    release.set()
    assert await lookup is ToolDiscovery.ABSENT
    assert process.fixed_zero_discovery is ToolDiscovery.ABSENT
    assert (old.list_calls, new.list_calls) == (1, 1)
    assert old.calls == new.calls == []


@pytest.mark.asyncio
async def test_discovered_tool_can_refuse_driver_capability() -> None:
    adapter, process, session = await _prepared(_result(_payload(_ROWS[1])))
    assert process.fixed_zero_discovery is ToolDiscovery.DISCOVERED
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.REFUSED_CAPABILITY, ContainmentReason.CAPABILITY_ABSENT
    )
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_command_state_returned_projection_not_physical() -> None:
    adapter, _, _ = await _prepared(_result(_payload()))
    result = await adapter.contain_heat_fixed_zero()
    assert vars(result) == {
        "outcome": ContainmentOutcome.COMMAND_STATE_RETURNED,
        "reason": None,
        "evidence_heat_zero_commanded": True,
        "evidence_is_pre_fallback": False,
    }


@pytest.mark.asyncio
async def test_unknown_completion_marked_pre_fallback_not_latest() -> None:
    adapter, _, _ = await _prepared(_result(_payload(_ROWS[-1])))
    result = await adapter.contain_heat_fixed_zero()
    assert result.outcome is ContainmentOutcome.UNKNOWN_COMPLETION
    assert result.evidence_heat_zero_commanded is True
    assert result.evidence_is_pre_fallback is True


def _stdio_process(tmp_path: Path, mode: str) -> MCPServerProcess:
    script = Path(__file__).parent / "fixtures" / "fixed_zero_stdio_peer.py"
    (tmp_path / "start.json").write_text(
        json.dumps({"session": {**SESSION_STATE_PAYLOAD, "session_id": _SESSION}})
    )
    (tmp_path / "response.json").write_text(json.dumps(_payload()))

    @asynccontextmanager
    async def factory(_params: StdioServerParameters) -> AsyncGenerator[InitializableSession]:
        params = StdioServerParameters(
            command=sys.executable, args=[str(script), mode, str(tmp_path)]
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            yield session

    return MCPServerProcess(MCPConfig(call_timeout_seconds=1.5), session_factory=factory)


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls").read_text().splitlines()


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.slow
@pytest.mark.serial  # Real stdio child and synchronous handler blockage.
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_real_stdio_timeout_leaves_remote_completion_unknown(
    tmp_path: Path, mode: str
) -> None:
    sdk = f"mcp={version('mcp')}, pydantic={version('pydantic')}, handler={mode}"
    process = _stdio_process(tmp_path, mode)
    await process.start()
    try:
        assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED, sdk
        adapter = RoasterControlAdapter.from_process(process)
        await adapter.start_session()
        lists_before = _calls(tmp_path).count("list")
        async with asyncio.TaskGroup() as tasks:
            attempt = tasks.create_task(adapter.contain_heat_fixed_zero())
            await _wait_for_file(tmp_path / "entered")
            if mode == "sync":
                with pytest.raises(MCPToolTimeoutError):
                    await process.call_tool("probe", {})
                assert "probe" not in _calls(tmp_path), sdk
            else:
                assert await process.call_tool("probe", {}) == {"ok": True, "serviced": True}, sdk
                assert "probe" in _calls(tmp_path), sdk
            assert await attempt == ContainmentResult(ContainmentOutcome.UNKNOWN_TRANSPORT), sdk
            assert not (tmp_path / "returned").exists(), sdk
        assert _calls(tmp_path).count(_TOOL) == 1, sdk
        assert _calls(tmp_path).count("list") == lists_before, sdk
        (tmp_path / "release").touch()
        await _wait_for_file(tmp_path / "returned")
        assert adapter.last_containment_result == ContainmentResult(
            ContainmentOutcome.UNKNOWN_TRANSPORT
        ), sdk
        assert process.running, sdk
    finally:
        (tmp_path / "release").touch()
        await process.stop()


@pytest.mark.slow
@pytest.mark.serial  # Real stdio child and deliberately malformed wire responses.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("scalar", ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE),
        ("null", ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE),
        ("missing", ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE),
        ("outer-error", ContainmentOutcome.UNKNOWN_TRANSPORT),
        ("valid", ContainmentOutcome.COMMAND_STATE_RETURNED),
    ],
)
async def test_real_stdio_uses_raw_structured_member_without_implicit_listing(
    tmp_path: Path, mode: str, outcome: ContainmentOutcome
) -> None:
    process = _stdio_process(tmp_path, mode)
    await process.start()
    try:
        assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
        adapter = RoasterControlAdapter.from_process(process)
        await adapter.start_session()
        before = _calls(tmp_path)
        assert (await adapter.contain_heat_fixed_zero()).outcome is outcome
        assert _calls(tmp_path) == [*before, _TOOL]
    finally:
        await process.stop()


@pytest.mark.slow
@pytest.mark.serial  # Real stdio process using the pinned MCP mock driver.
@pytest.mark.asyncio
async def test_dev_pin_does_not_advertise_fixed_zero() -> None:
    process = MCPServerProcess(
        MCPConfig(
            env={
                "COFFEE_ROASTER_DRIVER": "mock",
                "COFFEE_FIRST_CRACK_MODE": "disabled",
            }
        )
    )
    assert version("coffee-roaster-mcp") == "0.2.4"
    await process.start()
    try:
        assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    finally:
        await process.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("temperature", "counter", "counter_class"),
    [
        (196, 23, CounterClass.PRESENT),
        (196.0, 23, CounterClass.PRESENT),
        (196.0, "23", CounterClass.INVALID),
        (196.0, True, CounterClass.INVALID),
        (196.0, 23.0, CounterClass.INVALID),
        (196.0, ..., CounterClass.ABSENT),
        ("196", 23, CounterClass.PRESENT),
        (True, 23, CounterClass.PRESENT),
        (float("nan"), 23, CounterClass.PRESENT),
    ],
)
async def test_human_evidence_reads_once_and_captures_request_admission(
    temperature: object, counter: object, counter_class: CounterClass
) -> None:
    payload = deepcopy(SESSION_STATE_PAYLOAD)
    device = cast("dict[str, object]", payload["device_state"])
    device["bean_temp_c"] = temperature
    vendor: dict[str, object] = {"ignored_temperature_packet_count": 2}
    if counter is not ...:
        vendor["status_packet_count"] = counter
    device["raw_vendor_data"] = vendor
    reads: list[str] = []
    now = 10.0
    clocks: list[float] = []

    async def call(name: str, arguments: dict[str, object]) -> object:
        nonlocal now
        reads.append(name)
        assert arguments == {}
        now += 3
        return payload

    def clock() -> float:
        clocks.append(now)
        return now

    adapter = RoasterControlAdapter(RoasterMCPClient(call), clock=clock)
    telemetry, counters, admitted_at = await adapter.read_human_evidence()
    assert reads == ["get_roast_state"]
    assert admitted_at == 10 and clocks == [10, 13]
    assert counters.class_ is counter_class
    assert adapter.last_state is not None
    if type(temperature) in (int, float) and temperature == 196:
        assert telemetry is not None and telemetry.bean_temp_c == 196
    else:
        assert telemetry is None


@pytest.mark.asyncio
async def test_legacy_telemetry_neither_extracts_counters_nor_adds_clock_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = deepcopy(SESSION_STATE_PAYLOAD)
    device = cast("dict[str, object]", payload["device_state"])
    device["raw_vendor_data"] = {
        "status_packet_count": True,
        "ignored_temperature_packet_count": "9",
    }
    session = _Session(_result(payload))
    process = MCPServerProcess(session=session)
    clocks: list[float] = []

    def clock() -> float:
        clocks.append(17)
        return 17

    def unexpected_extraction(_raw: object) -> object:
        pytest.fail("legacy path extracted accepted counters")

    monkeypatch.setattr(adapter_module, "extract_accepted_counters", unexpected_extraction)
    adapter = RoasterControlAdapter.from_process(process, clock=clock)
    telemetry = await adapter.read_telemetry()
    assert telemetry is not None and telemetry.bean_temp_c == 196
    assert clocks == [17]
    assert session.list_calls == 0
    assert session.calls == [("get_roast_state", {})]


@pytest.mark.asyncio
async def test_without_advertisement_or_session_no_fixed_zero_is_dispatched() -> None:
    session = _Session(_result(_payload()), ListToolsResult(tools=[]))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    await adapter.start_session()
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert session.calls == [("start_roast_session", {})]

    legacy = RoasterControlAdapter(RoasterMCPClient(process.call_tool))
    assert (await legacy.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert session.calls == [("start_roast_session", {})]


@pytest.mark.asyncio
async def test_identity_reset_and_child_detach_invalidate_containment() -> None:
    adapter, process, session = await _prepared(_result(_payload()))
    adapter.reset_session_identity_for_new_run()
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    process.replace(None)
    assert process.fixed_zero_discovery is ToolDiscovery.ABSENT
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert session.calls == []


@pytest.mark.asyncio
async def test_replacement_during_containment_keeps_result_unknown() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowResult(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == _TOOL:
                entered.set()
                await release.wait()
            return await super().call_tool(name, arguments)

    old = SlowResult(_result(_payload()))
    new = _Session(_result(_payload()))
    process = _LifecycleSpy(old)
    adapter = RoasterControlAdapter.from_process(process)
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    await adapter.start_session()
    async with asyncio.TaskGroup() as tasks:
        attempt = tasks.create_task(adapter.contain_heat_fixed_zero())
        await asyncio.wait_for(entered.wait(), timeout=2)
        process.replace(new)
        release.set()
        assert (await attempt).outcome is ContainmentOutcome.UNKNOWN_TRANSPORT
    assert [name for name, _ in old.calls] == ["start_roast_session", _TOOL]
    assert new.calls == []
    assert process.lifecycle == []
