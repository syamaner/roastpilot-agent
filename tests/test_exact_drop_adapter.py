"""Private exact Drop discovery and dispatch using a hardware-free fake SDK."""

import ast
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from test_exact_drop_reply import IDENTITY, ROWS, payload
from test_timing_evidence_consumer import Session, golden, internal, reply, schema

import roastpilot_agent.mcp_client as mcp_client
from roastpilot_agent.config import MCPConfig
from roastpilot_agent.exact_drop_reply import ExactDropDisposition as D
from roastpilot_agent.exact_drop_reply import ExactDropIdentity
from roastpilot_agent.human_protection import ToolDiscovery as T
from roastpilot_agent.mcp_client import MCPServerProcess, RoasterControlAdapter
from roastpilot_agent.timing_evidence import TimingEvidenceReadOutcome


def drop_schema() -> dict[str, Any]:
    """Build the ratified protected input schema, with presentation metadata."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(IDENTITY.arguments()),
        "properties": {
            "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "clock_epoch_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "ownership_sequence": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1},
            "timing_evidence_sequence": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1},
        },
        "title": "Private synthetic Drop",
        "description": "No physical qualification",
    }


def inventory(shape: Any = None) -> ListToolsResult:
    """Advertise timing and Drop on the same inventory."""
    return ListToolsResult(
        tools=[
            Tool(name="get_timing_evidence", inputSchema=schema()),
            Tool(
                name="drop_beans_exact_session",
                inputSchema=drop_schema() if shape is None else shape,
            ),
        ]
    )


def drop_reply(raw: Any = None) -> CallToolResult:
    """Supply an explicit false error member for protected source-derived rows."""
    raw = payload() if raw is None else raw
    return CallToolResult(
        isError=False,
        content=[TextContent(type="text", text=json.dumps(raw))],
        structuredContent=raw,
    )


async def prepared() -> tuple[RoasterControlAdapter, MCPServerProcess, Session]:
    """Start an owned fake session and admit actual decoded synthetic evidence."""
    session = Session(reply(golden()), inventory())
    process = MCPServerProcess(session=session)
    adapter = RoasterControlAdapter.from_process(process)
    assert process.exact_drop_discovery is T.ABSENT
    assert await process.discover_timing_evidence() is T.DISCOVERED
    assert process.exact_drop_discovery is T.DISCOVERED
    await adapter.start_session()
    assert (await adapter.read_timing_evidence()).outcome is TimingEvidenceReadOutcome.EVIDENCE
    internal(session)._result = drop_reply()
    session.calls.clear()
    return adapter, process, session


async def dispatch(adapter: RoasterControlAdapter, identity: ExactDropIdentity = IDENTITY) -> Any:
    """Access the deliberately private S1 seam only in tests."""
    return await internal(adapter)._drop_beans_exact_session(identity)


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "timing-only",
        "duplicate",
        "paginated",
        "duplicate-required",
        "extra-required",
        "required-type",
        "required-unhashable",
        "extra-property",
        "extra-semantic",
        "property-semantic",
        "boolean-minimum",
        "float-maximum",
        "bound-drift",
        "pattern-drift",
        "property-type",
        "additional-properties",
        "missing-required",
        "missing-property",
        "malformed-properties",
        "malformed-inventory",
        "malformed-tools",
        "malformed-tools-container",
        "malformed-drop",
        "malformed-timing",
        "description-only-extra",
        "reordered",
    ],
)
@pytest.mark.asyncio
async def test_discovery_is_independent_and_terminal(case: str) -> None:
    shape = drop_schema()
    inv: Any = inventory(shape)
    props = shape["properties"]
    valid = case in ("exact", "description-only-extra", "reordered", "malformed-timing")
    if case == "timing-only":
        inv.tools.pop()
    elif case == "duplicate":
        inv.tools.append(inv.tools[-1])
    elif case == "paginated":
        inv.nextCursor = "next"
    elif case == "duplicate-required":
        shape["required"].append("session_id")
    elif case == "extra-required":
        shape["required"][-1] = "foreign"
    elif case == "required-type":
        shape["required"] = "session_id"
    elif case == "required-unhashable":
        shape["required"][0] = []
    elif case == "extra-property":
        props["extra"] = {"type": "string"}
    elif case == "extra-semantic":
        shape["oneOf"] = []
    elif case == "property-semantic":
        props["session_id"]["default"] = "foreign"
    elif case == "boolean-minimum":
        props["ownership_sequence"]["minimum"] = True
    elif case == "float-maximum":
        props["timing_evidence_sequence"]["maximum"] = float(2**53 - 1)
    elif case == "bound-drift":
        props["session_id"]["maxLength"] = 129
    elif case == "pattern-drift":
        props["clock_epoch_id"]["pattern"] = "[0-9a-f]{32}"
    elif case == "property-type":
        props["ownership_sequence"]["type"] = "number"
    elif case == "additional-properties":
        shape["additionalProperties"] = 0
    elif case == "missing-required":
        del shape["required"]
    elif case == "missing-property":
        del props["session_id"]
    elif case == "malformed-properties":
        shape["properties"] = []
    elif case == "malformed-inventory":
        inv = {"tools": []}
    elif case == "malformed-tools":
        inv = ListToolsResult.model_construct(tools=[None])
    elif case == "malformed-tools-container":
        inv = ListToolsResult.model_construct(tools=tuple(inventory().tools))
    elif case == "malformed-drop":
        inv.tools[-1] = Tool.model_construct(name="drop_beans_exact_session", inputSchema=None)
    elif case == "malformed-timing":
        inv.tools[0].inputSchema = {}
    elif case == "description-only-extra":
        for prop in props.values():
            prop.update(title="Display", description="Display")
    elif case == "reordered":
        shape["required"].reverse()
    # Rebuild: SDK models may copy the original dictionary.
    if case not in (
        "timing-only",
        "duplicate",
        "paginated",
        "malformed-inventory",
        "malformed-tools",
        "malformed-tools-container",
        "malformed-drop",
        "malformed-timing",
    ):
        inv = inventory(shape)
    session = Session(reply(golden()), inv)
    process = MCPServerProcess(session=session)
    timing = await process.discover_timing_evidence()
    expected_timing = (
        T.ABSENT
        if case in ("paginated", "malformed-inventory", "malformed-tools", "malformed-timing")
        else T.DISCOVERED
    )
    assert timing is expected_timing
    assert process.exact_drop_discovery is (T.DISCOVERED if valid else T.ABSENT)
    await process.discover_timing_evidence()
    assert session.list_calls == 1
    assert session.calls == []


@pytest.mark.asyncio
async def test_old_discovery_cannot_publish_on_new_child() -> None:
    old = Session(reply(golden()), inventory())
    old.discovery_gate = asyncio.Event()
    process = MCPServerProcess(session=old)
    task = asyncio.create_task(process.discover_timing_evidence())
    await asyncio.sleep(0)
    assert old.list_calls == 1
    new = Session(reply(golden()), ListToolsResult(tools=[]))
    internal(process)._replace_session(new)
    assert process.exact_drop_discovery is T.ABSENT
    assert await process.discover_timing_evidence() is T.ABSENT
    old.discovery_gate.set()
    assert await task is T.ABSENT
    assert process.exact_drop_discovery is T.ABSENT
    assert process.timing_evidence_discovery is T.ABSENT
    internal(process)._replace_session(old)
    assert process.exact_drop_discovery is T.ABSENT
    assert process.timing_evidence_discovery is T.ABSENT


@pytest.mark.parametrize(
    "gate",
    [
        "transport",
        "discovery",
        "child",
        "session",
        "detached",
        "unavailable-sdk",
        "no-pin",
        "pin-type",
        "pin-session",
        "pin-child",
        "pin-clock",
        "pin-owner",
        "pin-evidence",
        "pin-invalid",
        "argument-type",
        "argument-bool",
        "argument-clock",
        "argument-session",
    ],
)
@pytest.mark.asyncio
async def test_local_refusals_never_enter_sdk(gate: str) -> None:
    adapter, process, session = await prepared()
    identity: Any = IDENTITY
    a = internal(adapter)
    if gate == "transport":
        a._fixed_zero_transport = None
    elif gate == "discovery":
        internal(process)._exact_drop_discovery = T.ABSENT
    elif gate == "child":
        a._started_child_epoch += 1
    elif gate == "session":
        a._started_session_id = "foreign"
    elif gate == "unavailable-sdk":
        internal(process)._session = None
    elif gate == "detached":
        internal(process)._replace_session(None)
    elif gate == "no-pin":
        a._timing_evidence_pin = None
    elif gate == "pin-type":
        a._timing_evidence_pin = object()
    elif gate.startswith("pin-"):
        field, value = {
            "pin-session": ("session_id", "foreign"),
            "pin-child": ("child_epoch", 2),
            "pin-clock": ("epoch_id", "b" * 32),
            "pin-owner": ("session_ownership_sequence", 2),
            "pin-evidence": ("evidence_sequence", 2),
            "pin-invalid": ("evidence_sequence", True),
        }[gate]
        a._timing_evidence_pin = replace(
            a._timing_evidence_pin, record=replace(a._timing_evidence_pin.record, **{field: value})
        )
    else:
        identity = {
            "argument-type": None,
            "argument-bool": replace(IDENTITY, ownership_sequence=True),
            "argument-clock": replace(IDENTITY, clock_epoch_id="invalid"),
            "argument-session": replace(IDENTITY, session_id="foreign"),
        }[gate]
    result = await dispatch(adapter, identity)
    assert result.disposition is D.NOT_SENT
    assert result.identity is None
    assert session.calls == []
    assert session.list_calls == 1


@pytest.mark.parametrize(("expected", "row"), ROWS)
@pytest.mark.asyncio
async def test_one_call_precise_arguments_and_closed_receipt(
    expected: D, row: tuple[object, ...]
) -> None:
    adapter, _, session = await prepared()
    internal(session)._result = drop_reply(payload(row))
    result = await dispatch(adapter)
    assert result.disposition is expected
    assert session.calls == [("drop_beans_exact_session", IDENTITY.arguments())]
    assert session.list_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "observation",
        "reset",
        "generation",
        "owner",
        "child",
        "transport",
        "loss",
        "malformed",
        "cancel",
        "timeout",
    ],
)
@pytest.mark.asyncio
async def test_in_flight_attribution_and_cancellation(change: str) -> None:
    adapter, process, session = await prepared()
    if change == "timeout":
        internal(process)._config = MCPConfig(call_timeout_seconds=0.01)
    session.waiters = []
    task = asyncio.create_task(dispatch(adapter))
    assert await session.entered.get() == 0
    blocked = session.waiters[0]
    original = internal(adapter)._timing_evidence_pin
    if change == "observation":
        # A later admitted observation does not displace the owner of this send.
        raw = golden()
        raw["record"]["evidence_sequence"] += 1
        internal(session)._result = reply(raw)
        session.waiters = None
        assert (await adapter.read_timing_evidence()).outcome is TimingEvidenceReadOutcome.EVIDENCE
        session.waiters = []
    elif change == "reset":
        adapter.reset_session_identity_for_new_run()
    elif change == "generation":
        internal(adapter)._identity_generation += 1
    elif change == "owner":
        internal(adapter)._started_session_id = "replacement"
    elif change == "child":
        internal(process)._replace_session(Session(drop_reply(), inventory()))
    elif change == "transport":
        internal(adapter)._fixed_zero_transport = MCPServerProcess(session=session)
    if change == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif change == "timeout":
        assert (await task).disposition is D.UNKNOWN
    else:
        future = blocked
        if change == "loss":
            future.set_exception(ConnectionError("synthetic loss"))
        else:
            future.set_result(object() if change == "malformed" else drop_reply())
        result = await task
        expected = D.COMPLETED if change in ("none", "observation") else D.UNKNOWN
        assert result.disposition is expected
        assert result.identity == (IDENTITY if expected is D.COMPLETED else None)
    assert [name for name, _ in session.calls].count("drop_beans_exact_session") == 1
    assert session.list_calls == 1
    if change not in ("observation", "reset"):
        assert internal(adapter)._timing_evidence_pin is original


def test_application_caller_absence() -> None:
    """Keep the private declaration unique and unused throughout application source."""
    module_path = Path(mcp_client.__file__).resolve()
    source_paths = set(module_path.parent.rglob("*.py"))
    assert module_path.name == "mcp_client.py"
    assert module_path in source_paths
    declarations: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    private_name = "_drop_beans_exact_session"
    for path in sorted(source_paths):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == private_name:
                    declarations.append((path, node))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                assert node.attr != private_name, (path, node.lineno)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                assert node.id != private_name, (path, node.lineno)
            elif isinstance(node, ast.Constant):
                assert node.value != private_name, (path, node.lineno)
    assert len(declarations) == 1
    declaration_path, declaration = declarations[0]
    assert declaration_path == module_path
    assert isinstance(declaration, ast.AsyncFunctionDef)
