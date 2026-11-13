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
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool
from pydantic import ValidationError
from test_mcp_client import SESSION_STATE_PAYLOAD, FakeSession

import roastpilot_agent.live as live_module
import roastpilot_agent.mcp_client as adapter_module
from roastpilot_agent.config import AppConfig, MCPConfig
from roastpilot_agent.human_protection import (
    ContainmentOutcome,
    ContainmentReason,
    ContainmentResult,
    CounterClass,
    CounterEvidence,
    RawTemperature,
    ToolDiscovery,
    extract_accepted_counters,
)
from roastpilot_agent.mcp_client import (
    InitializableSession,
    MCPConnectionError,
    MCPServerProcess,
    MCPToolError,
    MCPToolTimeoutError,
    RoasterControlAdapter,
    RoasterMCPClient,
    RoastSessionState,
    project_session_state,
)
from roastpilot_agent.replay import ReplayRoasterControl

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


async def _assert_rejected_containment_replaces_latest_result(
    adapter: RoasterControlAdapter, process: _LifecycleSpy, prior_session: _Session
) -> None:
    prior_calls = prior_session.calls.copy()
    replacement = _Session(_result(_payload()))
    process.replace(replacement)
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    rejected = await adapter.contain_heat_fixed_zero()
    assert rejected == ContainmentResult(ContainmentOutcome.NOT_ATTEMPTED)
    assert adapter.last_containment_result == rejected
    assert prior_session.calls == prior_calls
    assert replacement.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        pytest.param(_result(_payload()), ContainmentOutcome.COMMAND_STATE_RETURNED, id="success"),
        pytest.param(
            MCPToolTimeoutError("local timeout"), ContainmentOutcome.UNKNOWN_TRANSPORT, id="unknown"
        ),
    ],
)
async def test_containment_rejection_updates_cache_after_prior_attempt(
    reply: object, outcome: ContainmentOutcome
) -> None:
    adapter, process, session = await _prepared(reply)
    prior = await adapter.contain_heat_fixed_zero()
    assert prior.outcome is outcome
    assert adapter.last_containment_result == prior
    assert len(session.calls) == 1
    await _assert_rejected_containment_replaces_latest_result(adapter, process, session)


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
    await _assert_rejected_containment_replaces_latest_result(adapter, process, session)


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


def _non_object_or_missing_schema_property(name: str, value: object) -> Tool:
    schema = _schema()
    properties = cast("dict[str, object]", schema["properties"])
    if value is ...:
        del properties[name]
    else:
        properties[name] = value
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
            ListToolsResult(tools=[_changed_schema("type", "number", property_name)])
            for property_name in ("expected_session_id", "containment_protocol")
        ],
        *[
            ListToolsResult(tools=[_changed_schema(bound, ..., "expected_session_id")])
            for bound in ("minLength", "maxLength")
        ],
        *[
            ListToolsResult(tools=[_non_object_or_missing_schema_property(property_name, value)])
            for property_name in ("expected_session_id", "containment_protocol")
            for value in ("string", ...)
        ],
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


def _stdio_process(
    tmp_path: Path, mode: str, *, config: MCPConfig | None = None
) -> MCPServerProcess:
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

    if config is None:
        config = MCPConfig(call_timeout_seconds=1.5)
    return MCPServerProcess(config, session_factory=factory)


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
    config = MCPConfig()
    process = _stdio_process(tmp_path, mode, config=config)
    await process.start()
    try:
        assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED, sdk
        adapter = RoasterControlAdapter.from_process(process)
        await adapter.start_session()
        lists_before = _calls(tmp_path).count("list")
        # Apply the measured deadline after startup, discovery and Start.
        config.call_timeout_seconds = 1.5
        assert process._config.call_timeout_seconds == 1.5, sdk  # pyright: ignore[reportPrivateUsage]
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


_HUMAN_READ_CALL: list[tuple[str, dict[str, object]]] = [("get_roast_state", {})]


class _DecodedRead:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.now = 10.0
        self.clocks: list[float] = []
        self.client = RoasterMCPClient(self.call)
        self.adapter = RoasterControlAdapter(self.client, clock=self.clock)

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        self.now += 3
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    def clock(self) -> float:
        self.clocks.append(self.now)
        return self.now


def _state_payload(*, session_id: str = _SESSION) -> dict[str, object]:
    return {**deepcopy(SESSION_STATE_PAYLOAD), "session_id": session_id}


def _set_field(fields: dict[str, object], key: str, value: object) -> None:
    if value is ...:
        del fields[key]
    else:
        fields[key] = value


async def _assert_automatic_read(payload: object) -> None:
    reader = _DecodedRead(payload)
    try:
        expected = RoastSessionState.model_validate(payload)
    except (ValidationError, OverflowError) as error:
        with pytest.raises(type(error)):
            await reader.client.get_roast_state()
        with pytest.raises(type(error)):
            await reader.adapter.read_telemetry()
        assert reader.clocks == []
        assert reader.adapter.last_state is None
    else:
        assert await reader.client.get_roast_state() == expected
        assert await reader.adapter.read_telemetry() == project_session_state(
            expected, age_seconds=0, ambient_age_seconds=0
        )
        assert reader.clocks == [16]
        assert reader.adapter.last_state == expected
    assert reader.calls == _HUMAN_READ_CALL * 2


def _assert_projection_state(reader: _DecodedRead, expected: RoastSessionState) -> None:
    assert reader.adapter.last_state == expected
    assert reader.adapter._last_elapsed == expected.elapsed_monotonic_seconds  # pyright: ignore[reportPrivateUsage]
    assert reader.adapter._last_change_monotonic == 13  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["bean_temp_c", "env_temp_c"])
@pytest.mark.parametrize(
    ("temperature", "classification"),
    [
        pytest.param(196, RawTemperature.VALID, id="int"),
        pytest.param(196.0, RawTemperature.VALID, id="float"),
        pytest.param(2**53, RawTemperature.VALID, id="upper-bound"),
        pytest.param(-(2**53), RawTemperature.VALID, id="lower-bound"),
        pytest.param(2**53 + 1, RawTemperature.INVALID, id="above-bound"),
        pytest.param(-(2**53) - 1, RawTemperature.INVALID, id="below-bound"),
        pytest.param(10**400, RawTemperature.INVALID, id="huge"),
        pytest.param("196", RawTemperature.INVALID, id="string"),
        pytest.param(True, RawTemperature.INVALID, id="true"),
        pytest.param(False, RawTemperature.INVALID, id="false"),
        pytest.param([], RawTemperature.INVALID, id="array"),
        pytest.param({}, RawTemperature.INVALID, id="object"),
        pytest.param(float("nan"), RawTemperature.INVALID, id="nan"),
        pytest.param(float("inf"), RawTemperature.INVALID, id="positive-inf"),
        pytest.param(float("-inf"), RawTemperature.INVALID, id="negative-inf"),
        pytest.param(..., RawTemperature.ABSENT, id="missing"),
        pytest.param(None, RawTemperature.ABSENT, id="null"),
    ],
)
async def test_human_evidence_reads_once_and_captures_request_admission(
    key: str, temperature: object, classification: RawTemperature
) -> None:
    payload = _state_payload()
    device = cast("dict[str, object]", payload["device_state"])
    _set_field(device, key, temperature)
    device["raw_vendor_data"] = {"status_packet_count": 23, "ignored_temperature_packet_count": 2}
    before = deepcopy(payload)
    reader = _DecodedRead(payload)
    evidence = await reader.adapter.read_human_evidence()
    assert reader.calls == _HUMAN_READ_CALL
    assert evidence.request_admitted_at == 10
    assert reader.clocks == [10, 13]
    assert evidence.device_object
    assert evidence.bound_child_epoch is None
    assert evidence.counters == CounterEvidence(23, 2, CounterClass.PRESENT)
    if key == "bean_temp_c":
        measured, raw = evidence.bean, evidence.bean_temp_c
        assert evidence.env is RawTemperature.VALID and evidence.env_temp_c == 214.0
    else:
        measured, raw = evidence.env, evidence.env_temp_c
        assert evidence.bean is RawTemperature.VALID and evidence.bean_temp_c == 196.0
    assert measured is classification
    expected_temperature = None
    if classification is RawTemperature.VALID:
        assert raw is temperature
        assert type(raw) is type(temperature)
        assert raw is not None and evidence.telemetry is not None
        expected_temperature = float(raw)
        assert getattr(evidence.telemetry, key) == expected_temperature
    else:
        assert raw is None and evidence.telemetry is None
    assert payload == before
    _assert_projection_state(
        reader,
        RoastSessionState.model_validate(
            {**payload, "device_state": {**device, key: expected_temperature}}
        ),
    )
    await _assert_automatic_read(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_shape", [True, False])
async def test_human_evidence_validates_one_mirror_payload(
    monkeypatch: pytest.MonkeyPatch, valid_shape: bool
) -> None:
    payload = _state_payload()
    device = cast("dict[str, object]", payload["device_state"])
    device["bean_temp_c"] = "196"
    if not valid_shape:
        payload["device_state"] = "malformed-device"
    original_validate = RoastSessionState.model_validate
    seen: list[object] = []

    def validate(candidate: object) -> RoastSessionState:
        seen.append(candidate)
        return original_validate(candidate)

    monkeypatch.setattr(RoastSessionState, "model_validate", validate)
    reader = _DecodedRead(payload)
    if valid_shape:
        evidence = await reader.adapter.read_human_evidence()
        assert evidence.bean is RawTemperature.INVALID and evidence.telemetry is None
        assert len(seen) == 1
        expected = {**payload, "device_state": {**device, "bean_temp_c": None}}
        assert seen[0] == expected and seen[0] is not payload
    else:
        with pytest.raises(ValidationError):
            await reader.adapter.read_human_evidence()
        assert len(seen) == 1 and seen[0] is payload
    assert reader.calls == _HUMAN_READ_CALL


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [..., None], ids=["missing", "null"])
async def test_human_evidence_absent_device(device: object) -> None:
    payload = _state_payload()
    _set_field(payload, "device_state", device)
    before = deepcopy(payload)
    reader = _DecodedRead(payload)
    try:
        evidence = await reader.adapter.read_human_evidence()
    except ValidationError as error:
        pytest.fail(f"missing or null device must produce an envelope: {error}")
    assert reader.calls == _HUMAN_READ_CALL
    assert evidence.request_admitted_at == 10 and reader.clocks == [10, 13]
    assert not evidence.device_object
    assert evidence.bean is evidence.env is RawTemperature.ABSENT
    assert evidence.bean_temp_c is evidence.env_temp_c is evidence.telemetry is None
    assert evidence.counters == CounterEvidence(None, None, CounterClass.ABSENT)
    assert evidence.bound_child_epoch is None
    assert payload == before
    _assert_projection_state(
        reader, RoastSessionState.model_validate({**payload, "device_state": None})
    )
    await _assert_automatic_read(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "key", "value"),
    [
        pytest.param("top", "", [], id="top-list"),
        pytest.param("top", "", "state", id="top-string"),
        pytest.param("top", "", None, id="top-null"),
        pytest.param("session", "device_state", "device", id="device-string"),
        pytest.param("session", "device_state", 0, id="device-int"),
        pytest.param("session", "device_state", True, id="device-bool"),
        pytest.param("session", "device_state", [], id="device-list"),
        pytest.param("session", "session_id", ..., id="missing-id"),
        pytest.param("session", "phase", "unknown", id="unknown-phase"),
        pytest.param("device", "raw_vendor_data", [], id="vendor-list"),
        pytest.param("device", "raw_vendor_data", {"status_packet_count": []}, id="vendor-entry"),
        pytest.param("device", "heat_level_percent", "not-an-integer", id="heat"),
        pytest.param("mixed", "phase", "unknown", id="temperature-and-phase"),
    ],
)
async def test_human_evidence_preserves_non_temperature_failure(
    monkeypatch: pytest.MonkeyPatch, scope: str, key: str, value: object
) -> None:
    payload: object
    if scope == "top":
        payload = value
    else:
        state = _state_payload()
        device = cast("dict[str, object]", state["device_state"])
        _set_field(device if scope == "device" else state, key, value)
        if scope == "mixed":
            device["bean_temp_c"] = []
        payload = state

    before = deepcopy(payload)
    last = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    reader = _DecodedRead(payload)
    monkeypatch.setattr(reader.adapter, "_last_state", last)
    with pytest.raises(ValidationError) as original:
        RoastSessionState.model_validate(payload)
    with pytest.raises(type(original.value)):
        await reader.adapter.read_human_evidence()
    assert reader.calls == _HUMAN_READ_CALL
    assert reader.clocks == [10]
    assert reader.adapter.last_state is last
    assert payload == before
    await _assert_automatic_read(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        MCPToolTimeoutError("local timeout"),
        MCPConnectionError("pipe"),
        MCPToolError("tool error"),
        TimeoutError("deadline"),
        RuntimeError("runtime failure"),
        asyncio.CancelledError(),
    ],
)
async def test_human_evidence_propagates_read_failure(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    last = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    reader = _DecodedRead(failure)
    monkeypatch.setattr(reader.adapter, "_last_state", last)
    with pytest.raises(type(failure)) as result:
        await reader.adapter.read_human_evidence()
    assert result.value is failure
    assert reader.calls == _HUMAN_READ_CALL
    assert reader.clocks == [10]
    assert reader.adapter.last_state is last
    with pytest.raises(type(failure)) as automatic:
        await reader.adapter.read_telemetry()
    assert automatic.value is failure
    assert reader.calls == _HUMAN_READ_CALL * 2
    assert reader.clocks == [10]
    assert reader.adapter.last_state is last


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counter", "counter_class"),
    [
        (23, CounterClass.PRESENT),
        ("23", CounterClass.INVALID),
        (True, CounterClass.INVALID),
        (23.0, CounterClass.INVALID),
        (..., CounterClass.ABSENT),
    ],
)
async def test_human_evidence_extracts_raw_vendor_object(
    monkeypatch: pytest.MonkeyPatch, counter: object, counter_class: CounterClass
) -> None:
    payload = _state_payload()
    device = cast("dict[str, object]", payload["device_state"])
    vendor = {"status_packet_count": counter, "ignored_temperature_packet_count": 2}
    _set_field(vendor, "status_packet_count", counter)
    device["raw_vendor_data"] = vendor
    before = deepcopy(payload)
    seen: list[object] = []

    def extract(raw: dict[str, object] | None) -> CounterEvidence:
        seen.append(raw)
        return extract_accepted_counters(raw)

    monkeypatch.setattr(adapter_module, "extract_accepted_counters", extract)
    reader = _DecodedRead(payload)
    evidence = await reader.adapter.read_human_evidence()
    assert len(seen) == 1 and seen[0] is vendor
    assert evidence.counters.class_ is counter_class
    assert evidence.telemetry is not None
    assert reader.calls == _HUMAN_READ_CALL
    assert evidence.request_admitted_at == 10 and reader.clocks == [10, 13]
    assert payload == before
    monkeypatch.undo()
    await _assert_automatic_read(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["bound", "no-start", "different-session", "different-epoch"])
async def test_human_evidence_binds_only_to_same_start_and_child(case: str) -> None:
    payload = _state_payload(session_id="other" if case == "different-session" else _SESSION)
    session = _Session(_result(payload))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    if case != "no-start":
        await adapter.start_session()
        session.calls.clear()
    if case == "different-epoch":
        session = _Session(_result(payload))
        process.replace(session)
    evidence = await adapter.read_human_evidence()
    assert evidence.bound_child_epoch == (process.child_epoch if case == "bound" else None)
    assert evidence.telemetry is not None
    assert session.calls == _HUMAN_READ_CALL


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["replacement", "first-start", "new-session", "replacement-start"])
async def test_human_evidence_binding_does_not_follow_suspended_read(case: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SuspendedRead(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == "get_roast_state":
                entered.set()
                await release.wait()
            return await super().call_tool(name, arguments)

    session = SuspendedRead(_result(_state_payload()))
    replacement = _Session(_result(_state_payload()))
    process = _LifecycleSpy(replacement if case == "replacement-start" else session)
    adapter = RoasterControlAdapter.from_process(process)
    if case != "first-start":
        await adapter.start_session()
        session.calls.clear()
    if case == "replacement-start":
        replacement.calls.clear()
        process.replace(session)
    async with asyncio.TaskGroup() as tasks:
        read = tasks.create_task(adapter.read_human_evidence())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            if case == "replacement":
                process.replace(replacement)
            else:
                if case == "new-session":
                    session.session_id = "next-session"
                await adapter.start_session()
        finally:
            release.set()
        evidence = await read
    assert evidence.bound_child_epoch == (process.child_epoch if case == "new-session" else None)
    assert evidence.telemetry is not None
    assert session.calls == (
        _HUMAN_READ_CALL
        if case == "replacement"
        else [("start_roast_session", {}), *_HUMAN_READ_CALL]
    )
    assert replacement.calls == []


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


@pytest.mark.asyncio
async def test_late_start_identity_cannot_authorize_containment_on_replacement() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SuspendedStart(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == "start_roast_session":
                entered.set()
                await release.wait()
            return await super().call_tool(name, arguments)

    # Both children could return the same string. Only the first child started.
    old = SuspendedStart(_result(_payload()))
    replacement = _Session(_result(_payload()))
    process = _LifecycleSpy(old)
    adapter = RoasterControlAdapter.from_process(process)
    assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    async with asyncio.TaskGroup() as tasks:
        start = tasks.create_task(adapter.start_session())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            process.replace(replacement)
            assert await process.discover_fixed_zero() is ToolDiscovery.DISCOVERED
        finally:
            release.set()
        await start
    assert adapter.latest_fault_session_id == _SESSION
    assert (await adapter.contain_heat_fixed_zero()).outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert old.calls == [("start_roast_session", {})]
    assert replacement.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_overlapping_discovery_reserves_only_one_inventory_call() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SuspendedInventory(_Session):
        async def list_tools(self) -> object:
            # Count entry before waiting. An erroneous second call returns
            # promptly so the causal failure cannot be a harness timeout.
            self.list_calls += 1
            if self.list_calls == 1:
                entered.set()
                await release.wait()
            return self.inventory

    session = SuspendedInventory(None)
    process = _LifecycleSpy(session)
    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(process.discover_fixed_zero())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
            assert session.list_calls == 1
        finally:
            release.set()
        assert await first is ToolDiscovery.DISCOVERED
    assert session.calls == []
    assert process.lifecycle == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", ["unrecognised_reason", {"reason": "unknown_session"}])
async def test_unknown_reason_code_returns_malformed_evidence(reason_code: object) -> None:
    payload = _payload(_ROWS[0])
    payload["reason_code"] = reason_code
    adapter, process, session = await _prepared(_result(payload))
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    )
    assert len(session.calls) == 1
    assert process.lifecycle == []


@pytest.mark.asyncio
async def test_unknown_completion_requires_valid_prefallback_state() -> None:
    payload = _payload(_ROWS[-1])
    state = cast("dict[str, object]", payload["commanded_state"])
    state["heat_level_percent"] = 1
    adapter, _, session = await _prepared(_result(payload))
    assert await adapter.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    )
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_transport_without_session_returns_unknown() -> None:
    process = MCPServerProcess()
    result = await process._contain_heat_fixed_zero(_SESSION)  # pyright: ignore[reportPrivateUsage]
    assert result == ContainmentResult(ContainmentOutcome.UNKNOWN_TRANSPORT)
    assert not process.running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        object(),
        CallToolResult(content=[ImageContent(type="image", data="AA==", mimeType="image/png")]),
    ],
    ids=["absent-content", "nontext-content"],
)
async def test_legacy_call_without_readable_content_remains_typed(raw: object) -> None:
    session = FakeSession(raw)
    process = MCPServerProcess(session=session)
    with pytest.raises(MCPConnectionError, match="no structured or text content"):
        await process.call_tool("get_roast_state", {})
    assert session.calls == [("get_roast_state", {})]


@pytest.mark.asyncio
async def test_discovery_rejects_schema_with_too_many_semantic_members() -> None:
    schema = {**_schema(), "extra_a": None, "extra_b": None, "extra_c": None}
    session = _Session(None, ListToolsResult(tools=[_tool(schema)]))
    process = _LifecycleSpy(session)
    assert await process.discover_fixed_zero() is ToolDiscovery.ABSENT
    assert session.list_calls == 1
    assert session.calls == []


@pytest.mark.asyncio
async def test_replay_contains_no_live_fixed_zero_capability() -> None:
    replay = ReplayRoasterControl()
    assert await replay.contain_heat_fixed_zero() == ContainmentResult(
        ContainmentOutcome.NOT_ATTEMPTED
    )
    assert replay.commands == []


@pytest.mark.asyncio
async def test_live_composition_binds_discovery_and_start_to_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _Session(_result(_payload()))
    process = _LifecycleSpy(session)

    def process_factory(_config: MCPConfig, **_kwargs: object) -> MCPServerProcess:
        return process

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(live_module, "MCPServerProcess", process_factory)
    service, transport, store = await live_module.build_live_service(
        AppConfig(), store_path=tmp_path / "live.sqlite3"
    )
    try:
        assert transport is process
        assert session.list_calls == 0
        assert session.calls == []
        roaster = service._roaster  # pyright: ignore[reportPrivateUsage]
        assert isinstance(roaster, RoasterControlAdapter)
        assert await transport.discover_fixed_zero() is ToolDiscovery.DISCOVERED
        await roaster.start_session()
        assert await roaster.contain_heat_fixed_zero() == ContainmentResult(
            ContainmentOutcome.COMMAND_STATE_RETURNED, evidence_heat_zero_commanded=True
        )
        assert [name for name, _ in session.calls] == ["start_roast_session", _TOOL]
    finally:
        await transport.stop()
        await store.close()
