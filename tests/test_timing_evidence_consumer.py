"""Read-only Agent contracts against the exact synthetic P1 grammar."""

import ast
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import ValidationError
from test_mcp_client import SESSION_STATE_PAYLOAD, FakeSession

from roastpilot_agent.config import MCPConfig
from roastpilot_agent.human_protection import ToolDiscovery
from roastpilot_agent.mcp_client import (
    InitializableSession,
    MCPServerProcess,
    RoasterControlAdapter,
    RoasterMCPClient,
)
from roastpilot_agent.timing_evidence import (
    EventSource,
    InvalidChannel,
    ProducerReason,
    Qualification,
    TimingEvidence,
    UnavailableChannel,
    ValidChannel,
    admit_record,
    decode_timing_evidence_reply,
)
from roastpilot_agent.timing_evidence import (
    TimingEvidenceReadOutcome as O,
)

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = "schema-session"
TOOL = "get_timing_evidence"
P1_COMMIT = "ea89e4090ae408b7c18a070f4a6ff12857209567"
P1_TREE = "6edd69acc8fb8f8b9905aaee5d5592035286a040"
P1_MANIFEST = "6f3a3b2df3efae3acaec4a32235c0d7cd5be59d803e1c89d4da4f1e1b99c8fae"
P1_SERVER = "f3dcf03de5cdf49ef31222b7e1f2be6fe3dd7913cd1bbc7bf60d00f328dfb169"
P1_SESSION = "db2c0b53838ae985015c7636473f176233b5970da8a3919b7d09a194557d3148"


def internal(value: object) -> Any:
    """Expose test-only state for explicit race and malformed-value injection."""
    return value


def golden() -> dict[str, Any]:
    return json.loads((FIXTURES / "timing_evidence_v1_golden.json").read_text())


def reply(raw: Any, *, text: str | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(raw) if text is None else text)],
        structuredContent=raw,
    )


def decode(raw: Any, *, text: str | None = None) -> Any:
    return decode_timing_evidence_reply(
        False,
        [{"type": "text", "text": json.dumps(raw) if text is None else text}],
        raw,
        SESSION,
        1,
    )


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["expected_session_id", "evidence_protocol"],
        "properties": {
            "expected_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "evidence_protocol": {"type": "string", "const": "timing_evidence/v1"},
        },
    }


class Session(FakeSession):
    """Controllable transport with an inventory and observable read barrier."""

    def __init__(self, result: object, inventory: object = None) -> None:
        super().__init__(result)
        self.inventory = (
            ListToolsResult(tools=[Tool(name=TOOL, inputSchema=schema())])
            if inventory is None
            else inventory
        )
        self.list_calls = 0
        self.entered: asyncio.Queue[int] = asyncio.Queue()
        self.waiters: list[asyncio.Future[object]] | None = None
        self.discovery_gate: asyncio.Event | None = None

    async def list_tools(self) -> object:
        """Return the explicit inventory once requested."""
        self.list_calls += 1
        if self.discovery_gate is not None:
            await self.discovery_gate.wait()
        if isinstance(self.inventory, Exception):
            raise self.inventory
        return self.inventory

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        """Record calls; optionally hold independently releasable evidence reads."""
        if name == "start_roast_session":
            self.calls.append((name, arguments))
            return CallToolResult(
                content=[],
                structuredContent={"session": {**SESSION_STATE_PAYLOAD, "session_id": SESSION}},
            )
        if self.waiters is not None:
            self.calls.append((name, arguments))
            index = len(self.waiters)
            future = asyncio.get_running_loop().create_future()
            self.waiters.append(future)
            self.entered.put_nowait(index)
            return await future
        return await super().call_tool(name, arguments)


async def prepared(raw: Any = None) -> tuple[RoasterControlAdapter, MCPServerProcess, Session]:
    session = Session(reply(golden() if raw is None else raw))
    process = MCPServerProcess(session=session)
    adapter = RoasterControlAdapter.from_process(process)
    assert await process.discover_timing_evidence() is ToolDiscovery.DISCOVERED
    await adapter.start_session()
    session.calls.clear()
    return adapter, process, session


def assert_read_only(session: Session, count: int = 1) -> None:
    assert (
        session.calls
        == [(TOOL, {"expected_session_id": SESSION, "evidence_protocol": "timing_evidence/v1"})]
        * count
    )


def test_t1_golden_and_synthetic_point_bracket() -> None:
    raw = golden()
    result = decode(raw)
    assert result.outcome is O.EVIDENCE
    record = result.record
    assert isinstance(record, TimingEvidence)
    assert record.epoch_id == "0" * 32
    assert record.session_ownership_sequence == 2**53 - 1
    assert isinstance(record.bean, ValidChannel)
    assert record.bean.celsius.hex() == (196.00000000000003).hex()
    assert record.bean.acquisition_lo_seconds.hex() == (0.0).hex()
    assert record.charge is not None and record.charge.source is EventSource.MANUAL_MARK
    assert (
        record.first_crack is not None and record.first_crack.source is EventSource.MODEL_DETECTION
    )
    for key in ("bean", "environment"):
        raw["record"][key]["acquisition_hi_seconds"] = 0.0
    assert decode(raw).outcome is O.EVIDENCE  # FC may be later than this point.
    raw["record"]["first_crack"]["source"] = "unattributed"
    raw["record"]["first_crack"]["time_basis"] = "producer_canonical_record"
    assert decode(raw).record.first_crack.source is EventSource.UNATTRIBUTED


def layers() -> list[tuple[str, ...]]:
    return [
        (),
        ("record",),
        ("record", "clock"),
        ("record", "charge"),
        ("record", "first_crack"),
        ("record", "bean"),
        ("record", "environment"),
    ]


def at(raw: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        raw = raw[key]
    return raw


SHAPES = [
    (path, key, mode)
    for path in layers()
    for key in at(golden(), path)
    for mode in ("missing", "extra")
]
FLOATS = [
    ("record", slot, field)
    for slot in ("bean", "environment")
    for field in ("celsius", "acquisition_lo_seconds", "acquisition_hi_seconds")
] + [("record", slot, "canonical_seconds") for slot in ("charge", "first_crack")]
INTS = [("record", "session_ownership_sequence"), ("record", "evidence_sequence")] + [
    ("record", slot, "event_index") for slot in ("charge", "first_crack")
]
LITERALS = (
    [
        ("record", "driver"),
        ("record", "time_basis"),
        ("record", "physical_qualification"),
        ("record", "phase"),
        ("record", "clock", "domain"),
        ("record", "clock", "suspend_behaviour"),
    ]
    + [
        ("record", slot, key)
        for slot in ("charge", "first_crack")
        for key in ("kind", "source", "time_basis", "physical_time_bound")
    ]
    + [
        ("record", slot, key)
        for slot in ("bean", "environment")
        for key in ("status", "basis", "qualification")
    ]
)
EPOCHS = [("record", "clock", "epoch_id")] + [
    ("record", slot, "clock_epoch_id") for slot in ("charge", "first_crack", "bean", "environment")
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "key", "mode"), SHAPES)
async def test_t2_closed_keys(path: tuple[str, ...], key: str, mode: str) -> None:
    raw = golden()
    obj = at(raw, path)
    if mode == "missing":
        del obj[key]
    else:
        obj["extra"] = None
    adapter, _, session = await prepared(raw)
    assert (await adapter.read_timing_evidence()).outcome is O.MALFORMED_RESPONSE
    assert_read_only(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", FLOATS + INTS + LITERALS + EPOCHS)
@pytest.mark.parametrize("bad", [True, [], {}, None, "coercible", -1, 2**53])
async def test_t2_wrong_scalars(path: tuple[str, ...], bad: Any) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = bad
    adapter, _, session = await prepared(raw)
    assert (await adapter.read_timing_evidence()).outcome is O.MALFORMED_RESPONSE
    assert_read_only(session)


@pytest.mark.parametrize("path", FLOATS)
@pytest.mark.parametrize("bad", [0, 1, "1.0", float("nan"), float("inf"), -float("inf")])
def test_t2_exact_floats(path: tuple[str, ...], bad: Any) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = bad
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize("path", INTS)
@pytest.mark.parametrize("bad", [1.0, "1", True, 2**53, -1])
def test_t2_exact_integers(path: tuple[str, ...], bad: Any) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = bad
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize("path", EPOCHS)
@pytest.mark.parametrize("bad", ["0" * 32 + "\n", "A" * 32])
def test_t2_epoch_fullmatch(path: tuple[str, ...], bad: str) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = bad
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("evidence_protocol", "timing_evidence/v2"),
        ("evidence_protocol", True),
        ("evidence_protocol", 1),
        ("evidence_version", 2),
        ("evidence_version", True),
        ("evidence_version", 1.0),
        ("evidence_version", "1"),
        ("evidence_version", None),
    ],
)
def test_t2_unsupported_protocol(key: str, value: Any) -> None:
    raw = golden()
    raw[key] = value
    assert decode(raw).outcome is O.UNSUPPORTED_PROTOCOL


@pytest.mark.parametrize("path", LITERALS)
def test_t2_nested_literals(path: tuple[str, ...]) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = "invented"
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "mode",
    [
        "duplicate",
        "nan",
        "infinity",
        "overflow",
        "signed-zero",
        "float-int",
        "too-large",
        "text-only",
        "wrapper",
        "scalar",
        "error",
        "negative",
        "chronology",
        "bracket",
        "epochs",
        "record-absent",
        "non-evidence-record",
        "nested-list",
        "deep",
    ],
)
def test_t2_envelopes(mode: str) -> None:
    raw = golden()
    text = json.dumps(raw)
    if mode == "duplicate":
        text = text.replace('"evidence_version": 1', '"evidence_version": 1, "evidence_version": 1')
    elif mode in ("nan", "infinity", "overflow"):
        text = text.replace(
            "196.00000000000003", {"nan": "NaN", "infinity": "Infinity", "overflow": "1e999"}[mode]
        )
    elif mode == "signed-zero":
        text = text.replace('"celsius": 0.0', '"celsius": -0.0')
    elif mode == "float-int":
        text = text.replace('"celsius": 0.0', '"celsius": 0')
    elif mode == "too-large":
        text += " " * 16384
    elif mode == "text-only":
        assert (
            decode_timing_evidence_reply(
                False, [{"type": "text", "text": text}], None, SESSION, 1
            ).outcome
            is O.MALFORMED_RESPONSE
        )
        return
    elif mode == "error":
        assert (
            decode_timing_evidence_reply(
                True, [{"type": "text", "text": text}], raw, SESSION, 1
            ).outcome
            is O.MALFORMED_RESPONSE
        )
        return
    else:
        if mode == "wrapper":
            raw = {"result": raw}
        elif mode == "scalar":
            raw = 1
        elif mode == "negative":
            raw["record"]["charge"]["canonical_seconds"] = -1.0
        elif mode == "chronology":
            raw["record"]["first_crack"]["event_index"] = 0
        elif mode == "bracket":
            raw["record"]["bean"]["acquisition_lo_seconds"] = 2.0
        elif mode == "epochs":
            raw["record"]["charge"]["clock_epoch_id"] = "1" * 32
        elif mode == "record-absent":
            raw["record"] = None
        elif mode == "non-evidence-record":
            raw.update(outcome="unavailable", reason_code="driver_raised")
        elif mode == "nested-list":
            raw["record"]["bean"] = [[[[]]]]
        elif mode == "deep":
            text = "[" * 2000 + "0" + "]" * 2000
        if mode != "deep":
            text = json.dumps(raw)
    assert decode(raw, text=text).outcome is O.MALFORMED_RESPONSE


def test_t2_largest_record_and_total_admission() -> None:
    raw = golden()
    record = raw["record"]
    record["session_id"] = raw["requested_session_id"] = "x" * 128
    record["phase"] = "cooling"
    record["drop"] = {
        **record["first_crack"],
        "kind": "beans_dropped",
        "source": "unattributed",
        "time_basis": "producer_canonical_record",
        "event_index": 2**53 - 1,
        "canonical_seconds": sys.float_info.max,
    }
    for slot in ("bean", "environment"):
        for field in ("celsius", "acquisition_lo_seconds", "acquisition_hi_seconds"):
            record[slot][field] = sys.float_info.max
    record["evidence_sequence"] = 2**53 - 1
    assert len(json.dumps(raw)) < 16384
    assert (
        decode_timing_evidence_reply(
            False, [{"type": "text", "text": json.dumps(raw)}], raw, "x" * 128, 1
        ).outcome
        is O.EVIDENCE
    )
    good = decode(golden()).record
    malformed: list[Any] = [
        None,
        {},
        [],
        replace(good, bean=[]),
        replace(good, evidence_sequence=True),
        replace(good, first_crack=[]),
        replace(good, qualification="qualified"),
    ]
    for bad in malformed:
        assert admit_record(bad, None) == (O.MALFORMED_RESPONSE, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [("requested_session_id",), ("record", "session_id")])
async def test_t3_session_echo(path: tuple[str, ...]) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = "wrong"
    adapter, _, session = await prepared(raw)
    assert (await adapter.read_timing_evidence()).outcome is O.IDENTITY_REJECTED
    assert_read_only(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("owner", O.IDENTITY_REJECTED),
        ("epoch", O.IDENTITY_REJECTED),
        ("repeated", O.SEQUENCE_REJECTED),
        ("lower", O.SEQUENCE_REJECTED),
        ("bracket", O.SEQUENCE_REJECTED),
        ("charge", O.EVENT_IDENTITY_REJECTED),
        ("fc", O.EVENT_IDENTITY_REJECTED),
        ("vanished", O.EVENT_IDENTITY_REJECTED),
    ],
)
async def test_t3_latched_pin(change: str, expected: O) -> None:
    adapter, _, session = await prepared()
    assert (await adapter.read_timing_evidence()).outcome is O.EVIDENCE
    raw = golden()
    raw["record"]["evidence_sequence"] = 3
    r = raw["record"]
    if change == "owner":
        r["session_ownership_sequence"] -= 1
    elif change == "epoch":
        r["clock"]["epoch_id"] = "1" * 32
        for slot in ("charge", "first_crack", "bean", "environment"):
            r[slot]["clock_epoch_id"] = "1" * 32
    elif change == "repeated":
        r["evidence_sequence"] = 1
    elif change == "lower":
        # First advance so a positive lower sequence remains grammatically valid.
        advanced = golden()
        advanced["record"]["evidence_sequence"] = 4
        internal(session)._result = reply(advanced)
        assert (await adapter.read_timing_evidence()).outcome is O.EVIDENCE
    elif change == "bracket":
        for slot in ("bean", "environment"):
            r[slot]["acquisition_hi_seconds"] = 0.5
    elif change == "charge":
        r["charge"]["canonical_seconds"] = 0.25
    elif change == "fc":
        r["first_crack"]["canonical_seconds"] = 3.0
    elif change == "vanished":
        r["phase"], r["first_crack"] = "roasting", None
    internal(session)._result = reply(raw)
    assert (await adapter.read_timing_evidence()).outcome is expected
    raw = golden()
    raw["record"]["evidence_sequence"] = 5
    internal(session)._result = reply(raw)
    assert (await adapter.read_timing_evidence()).outcome is O.EVIDENCE
    assert_read_only(session, 4 if change == "lower" else 3)


def test_t3_bracket_survives_channel_absence() -> None:
    good = decode(golden()).record
    _, pin = admit_record(good, None)
    absent = replace(
        good,
        evidence_sequence=2,
        bean=UnavailableChannel(),
        environment=InvalidChannel("non_finite"),
    )
    result, pin = admit_record(absent, pin)
    assert result is O.EVIDENCE
    assert pin is not None
    assert pin.bracket == (0.0, 1.0)
    bad = replace(
        good,
        evidence_sequence=3,
        bean=ValidChannel(1.0, 0.0, 0.5),
        environment=UnavailableChannel(),
    )
    assert admit_record(bad, pin) == (O.SEQUENCE_REJECTED, pin)


ROWS = {
    "refused_identity": (
        "unknown_session not_latest_session session_faulted "
        "session_not_active session_purpose_not_eligible session_changed_during_read"
    ),
    "refused_capability": (
        "capability_unreadable capability_malformed "
        "capability_absent capability_version_unsupported composition_not_qualified"
    ),
    "refused_busy": "finalisation_in_progress drop_in_progress",
    "unavailable": (
        "clock_invalid driver_raised driver_result_invalid driver_not_connected "
        "clock_regression out_of_order_read sequence_exhausted record_invalid"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "reason", "driver_read"),
    [
        (row, reason, read)
        for row, reasons in ROWS.items()
        for reason in reasons.split()
        for read in ("not_invoked", "invoked")
    ],
)
async def test_t4_all_producer_rows(row: str, reason: str, driver_read: str) -> None:
    raw = golden()
    raw.update(outcome=row, reason_code=reason, driver_read=driver_read, record=None)
    allowed = (
        (
            row == "refused_identity"
            and (driver_read == "invoked") == (reason == "session_changed_during_read")
        )
        or (row == "refused_capability" and driver_read == "not_invoked")
        or row == "refused_busy"
        or (row == "unavailable" and (reason == "clock_invalid" or driver_read == "invoked"))
    )
    adapter, _, session = await prepared(raw)
    result = await adapter.read_timing_evidence()
    assert result.outcome is (O(row) if allowed else O.MALFORMED_RESPONSE)
    assert result.producer_reason is (ProducerReason(reason) if allowed else None)
    assert_read_only(session)


@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        ("invalid", "not_exact_float", InvalidChannel),
        ("invalid", "non_finite", InvalidChannel),
        ("unavailable", "reported_none", UnavailableChannel),
    ],
)
def test_t4_channel_union(status: str, reason: str, kind: type) -> None:
    raw = golden()
    raw["record"]["bean"] = {"status": status, "reason": reason}
    result = decode(raw)
    assert result.outcome is O.EVIDENCE
    assert type(result.record.bean) is kind
    assert result.record.bean.reason == reason
    raw["record"]["bean"]["extra"] = 1
    assert decode(raw).outcome is O.MALFORMED_RESPONSE
    del raw["record"]["bean"]["extra"]
    raw["record"]["bean"]["reason"] = "invented"
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate", ["transport", "discovery", "epoch", "empty", "long", "bool", "list", "none"]
)
async def test_t5_zero_send_pregate(gate: str) -> None:
    adapter, process, session = await prepared()
    if gate == "transport":
        adapter = RoasterControlAdapter(RoasterMCPClient(session.call_tool))
    elif gate == "discovery":
        internal(process)._timing_evidence_discovery = ToolDiscovery.ABSENT
    elif gate == "epoch":
        internal(adapter)._started_child_epoch = process.child_epoch + 1
    else:
        internal(adapter)._started_session_id = {
            "empty": "",
            "long": "a" * 129,
            "bool": True,
            "list": [],
            "none": None,
        }[gate]
    assert (await adapter.read_timing_evidence()).outcome is O.NOT_ATTEMPTED
    assert_read_only(session, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reset", "generation", "snapshot", "replacement"])
async def test_t6_identity_during_await(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, process, session = await prepared()
    assert (await adapter.read_timing_evidence()).outcome is O.EVIDENCE
    old_pin = internal(adapter)._timing_evidence_pin
    if change == "generation":
        # Isolate the explicit generation fence from the snapshot's redundant
        # generation member; each fence has its own behavioural witness.
        snapshot = internal(adapter)._identity_snapshot()
        monkeypatch.setattr(adapter, "_identity_snapshot", lambda: snapshot)
    session.waiters = []
    task = asyncio.create_task(adapter.read_timing_evidence())
    await session.entered.get()
    if change == "reset":
        adapter.reset_session_identity_for_new_run()
    elif change == "generation":
        internal(adapter)._identity_generation += 1
    elif change == "snapshot":
        internal(adapter)._started_session_id = "replaced"
    else:
        internal(process)._replace_session(Session(reply(golden())))
    session.waiters[0].set_result(reply(golden()))
    assert (await task).outcome is O.IDENTITY_REJECTED
    assert internal(adapter)._timing_evidence_pin is (None if change == "reset" else old_pin)
    assert_read_only(session, 2)


@pytest.mark.asyncio
async def test_t6_overlapping_reads_and_transport_epoch() -> None:
    adapter, process, session = await prepared()
    session.waiters = []
    first = asyncio.create_task(adapter.read_timing_evidence())
    await session.entered.get()
    second = asyncio.create_task(adapter.read_timing_evidence())
    await session.entered.get()
    raw = golden()
    raw["record"]["evidence_sequence"] = 2
    session.waiters[1].set_result(reply(raw))
    assert (await second).outcome is O.EVIDENCE
    session.waiters[0].set_result(reply(golden()))
    assert (await first).outcome is O.SEQUENCE_REJECTED
    assert internal(adapter)._timing_evidence_pin.record.evidence_sequence == 2
    task = asyncio.create_task(internal(process)._read_timing_evidence(SESSION))
    await session.entered.get()
    internal(process)._child_epoch += 1
    session.waiters[2].set_result(reply(golden()))
    assert (await task).outcome is O.TRANSPORT_UNAVAILABLE
    assert_read_only(session, 3)


@pytest.mark.asyncio
async def test_t6_cancel_propagates() -> None:
    adapter, _, session = await prepared()
    session.waiters = []
    task = asyncio.create_task(adapter.read_timing_evidence())
    await session.entered.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert internal(adapter)._timing_evidence_pin is None
    assert_read_only(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("not-returned"), O.TRANSPORT_UNAVAILABLE),
        (ValidationError.from_exception_data("envelope", []), O.MALFORMED_RESPONSE),
    ],
)
async def test_t2_transport_failures(failure: Exception, expected: O) -> None:
    adapter, _, session = await prepared()
    internal(session)._result = failure
    assert (await adapter.read_timing_evidence()).outcome is expected
    assert_read_only(session)


def test_t7_pure_import_boundary() -> None:
    import roastpilot_agent.timing_evidence as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    forbidden = (
        "mcp",
        "mcp_client",
        "controller",
        "runner",
        "safety",
        "dtr_timing",
        "human_run_ownership",
    )
    assert all(not any(token in name.split(".") for token in forbidden) for name in imports)
    assert list(Qualification) == [Qualification.SYNTHETIC_MOCK_ONLY]
    code = """import sys
import roastpilot_agent.timing_evidence as evidence
from pathlib import Path
assert Path(evidence.__file__).resolve() == Path(EXPECTED_MODULE).resolve()
for name in sys.modules:
    forbidden = {"mcp", "mcp_client", "controller", "runner", "safety",
                 "dtr_timing", "human_run_ownership"}
    assert not any(part in forbidden for part in name.split(".")), name
"""
    code = code.replace("EXPECTED_MODULE", repr(module.__file__))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def stdio_process(tmp_path: Path, mode: str) -> MCPServerProcess:
    (tmp_path / "start.json").write_text(
        json.dumps({"session": {**SESSION_STATE_PAYLOAD, "session_id": SESSION}})
    )
    (tmp_path / "response.json").write_text(json.dumps(golden()))

    @asynccontextmanager
    async def factory(_params: StdioServerParameters) -> AsyncGenerator[InitializableSession]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", str(FIXTURES / "timing_evidence_stdio_peer.py"), mode, str(tmp_path)],
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            yield session

    return MCPServerProcess(MCPConfig(call_timeout_seconds=10), session_factory=factory)


@pytest.mark.slow
@pytest.mark.serial
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid", "text-only", "error", "malformed-error", "scalar"])
async def test_t8_real_sdk_peer(tmp_path: Path, mode: str) -> None:
    sdk = f"mcp={version('mcp')}, pydantic={version('pydantic')}"
    process = stdio_process(tmp_path, mode)
    await process.start()
    try:
        assert await process.discover_timing_evidence() is ToolDiscovery.DISCOVERED, sdk
        adapter = RoasterControlAdapter.from_process(process)
        await adapter.start_session()
        baseline = (tmp_path / "calls").read_text().splitlines()
        result = await adapter.read_timing_evidence()
        assert result.outcome is (O.EVIDENCE if mode == "valid" else O.MALFORMED_RESPONSE), sdk
        assert (tmp_path / "calls").read_text().splitlines()[len(baseline) :] == [TOOL], sdk
        if mode == "valid":
            record = result.record
            assert record is not None, sdk
            assert isinstance(record.bean, ValidChannel), sdk
            assert (
                type(record.session_ownership_sequence) is int
                and record.session_ownership_sequence == 2**53 - 1
            ), sdk
            assert record.bean.celsius.hex() == (196.00000000000003).hex(), sdk
            assert (
                type(record.bean.acquisition_lo_seconds) is float
                and record.bean.acquisition_lo_seconds.hex() == (0.0).hex()
            ), sdk
            assert (
                type(record.bean.acquisition_hi_seconds) is float
                and record.bean.acquisition_hi_seconds.hex() == (1.0).hex()
            ), sdk
    finally:
        await process.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "reverse-required",
        "title",
        "bool-bound",
        "float-bound",
        "extra-root",
        "extra-property",
        "extra-scalar",
        "missing",
        "duplicate",
        "cursor",
        "exception",
        "non-object",
    ],
)
async def test_t9_discovery(case: str) -> None:
    s = schema()
    if case == "reverse-required":
        s["required"].reverse()
    elif case == "title":
        s["title"] = "safe"
        s["properties"]["expected_session_id"]["description"] = "safe"
    elif case == "bool-bound":
        s["properties"]["expected_session_id"]["minLength"] = True
    elif case == "float-bound":
        s["properties"]["expected_session_id"]["maxLength"] = 128.0
    elif case == "extra-root":
        s["anyOf"] = []
    elif case == "extra-property":
        s["properties"]["extra"] = {}
    elif case == "extra-scalar":
        s["properties"]["evidence_protocol"]["default"] = "timing_evidence/v1"
    tools = [Tool(name=TOOL, inputSchema=s)]
    if case == "missing":
        tools = []
    if case == "duplicate":
        tools *= 2
    inventory: object = ListToolsResult(
        tools=tools, nextCursor="next" if case == "cursor" else None
    )
    if case == "exception":
        inventory = RuntimeError("closed")
    if case == "non-object":
        inventory = []
    session = Session(reply(golden()), inventory)
    process = MCPServerProcess(session=session)
    expected = (
        ToolDiscovery.DISCOVERED
        if case in ("valid", "reverse-required", "title")
        else ToolDiscovery.ABSENT
    )
    assert await process.discover_timing_evidence() is expected
    assert await process.discover_timing_evidence() is expected
    assert session.list_calls == 1
    assert session.calls == []


@pytest.mark.asyncio
async def test_t9_discovery_replacement_and_reservation() -> None:
    old = Session(reply(golden()))
    old.discovery_gate = asyncio.Event()
    process = MCPServerProcess(session=old)
    task = asyncio.create_task(process.discover_timing_evidence())
    await asyncio.sleep(0)
    assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
    new = Session(reply(golden()), ListToolsResult(tools=[]))
    internal(process)._replace_session(new)
    assert internal(process)._timing_evidence_discovery is None
    old.discovery_gate.set()
    assert await task is ToolDiscovery.ABSENT
    assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
    assert (old.list_calls, new.list_calls) == (1, 1)
    assert old.calls == new.calls == []


def verify_p1(root: Path) -> None:
    assert root.is_absolute() and root.name == "src" and root.is_dir()
    checkout = root.parent

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(checkout), *args],
            text=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).strip()

    assert git("rev-parse", "HEAD") == P1_COMMIT
    assert git("rev-parse", "HEAD^{tree}") == P1_TREE
    assert git("status", "--porcelain", "--ignored") == ""
    assert (
        git("symbolic-ref", "-q", "--short", "HEAD") == ""
        if False
        else git("branch", "--show-current") == ""
    )
    package = root / "coffee_roaster_mcp"
    manifest = "".join(
        f"{p.relative_to(package).as_posix()} {hashlib.sha256(p.read_bytes()).hexdigest()}\n"
        for p in sorted(package.rglob("*"))
        if p.is_file()
    )
    assert hashlib.sha256(manifest.encode("ascii")).hexdigest() == P1_MANIFEST
    assert hashlib.sha256((package / "mcp_server.py").read_bytes()).hexdigest() == P1_SERVER
    assert hashlib.sha256((package / "session.py").read_bytes()).hexdigest() == P1_SESSION


@pytest.mark.slow
@pytest.mark.serial
@pytest.mark.asyncio
async def test_t10_protected_p1_registered_child(tmp_path: Path) -> None:
    configured = os.environ.get("ROASTPILOT_P1_MCP_SRC")
    if configured is None:
        pytest.skip(
            "T10 requires exact protected P1 via absolute ROASTPILOT_P1_MCP_SRC; CI does not run it"
        )
    root = Path(configured)
    verify_p1(root)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import runpy\nfrom pathlib import Path\n"
        + f"scope = runpy.run_path({str(FIXTURES / 'timing_evidence_stdio_peer.py')!r})\n"
        + f"scope['instrument_p1'](Path({str(tmp_path)!r}))\n"
    )
    config = tmp_path / "mock.yaml"
    config.write_text(
        "roaster:\n  driver: mock\nfirst_crack:\n  mode: disabled\n"
        "  event_authority: automatic\nsession:\n  auto_t0_detection_enabled: false\n"
    )

    @asynccontextmanager
    async def factory(_params: StdioServerParameters) -> AsyncGenerator[InitializableSession]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "coffee_roaster_mcp.cli", "serve", "--config", str(config)],
            cwd=tmp_path,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join((str(injection), str(root))),
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            yield session

    process = MCPServerProcess(MCPConfig(call_timeout_seconds=20), session_factory=factory)
    await process.start()
    try:
        identity = json.loads((tmp_path / "identity.json").read_text())
        for key in ("package", "server", "drivers"):
            assert Path(identity[key]).resolve().is_relative_to(root.resolve()), identity
        assert identity["versions"] == {
            "mcp": "1.30.0",
            "pydantic": "2.13.5",
            "pydantic-core": "2.46.5",
        }, identity
        assert identity["self_test"] == 1
        assert set(identity["wrapped"]) == {
            "set_heat",
            "set_fan",
            "drop_beans",
            "start_cooling",
            "stop_cooling",
            "emergency_stop",
            "contain_heat_fixed_zero",
        }
        assert await process.discover_timing_evidence() is ToolDiscovery.DISCOVERED
        adapter = RoasterControlAdapter.from_process(process)
        await adapter.start_session()
        await process.call_tool("mark_beans_added", {})
        await process.call_tool("mark_first_crack", {})

        def counts() -> dict[str, int]:
            return json.loads((tmp_path / "actuators.json").read_text())

        baseline = counts()
        assert (await adapter.read_timing_evidence()).outcome is O.EVIDENCE
        assert counts() == baseline
        (tmp_path / "fail-next-evidence").touch()
        result = await adapter.read_timing_evidence()
        assert result.outcome is O.PRODUCER_UNAVAILABLE
        assert result.producer_reason is ProducerReason.DRIVER_RAISED
        assert counts() == baseline
        await process.call_tool("drop_beans", {})
        await process.call_tool("start_cooling", {})
        await process.call_tool("stop_cooling", {})
        await process.call_tool("start_roast_session", {})
        baseline = counts()
        result = await adapter.read_timing_evidence()
        assert result.outcome is O.REFUSED_IDENTITY
        assert result.producer_reason is ProducerReason.NOT_LATEST_SESSION
        assert counts() == baseline
        print(
            "T10 non-skip: protected commit/tree/manifest matched; paths and versions",
            identity,
            "; EVIDENCE, driver_raised, not_latest_session; all actuator deltas zero",
        )
    finally:
        await process.stop()
        verify_p1(root)


@pytest.mark.slow
@pytest.mark.serial
@pytest.mark.asyncio
async def test_t9_installed_legacy_024_absence() -> None:
    assert version("coffee-roaster-mcp") == "0.2.4"
    process = MCPServerProcess(MCPConfig(call_timeout_seconds=20))
    await process.start()
    try:
        adapter = RoasterControlAdapter.from_process(process)
        assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
        assert (await adapter.read_timing_evidence()).outcome is O.NOT_ATTEMPTED
    finally:
        await process.stop()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("record", "bean", "clock_epoch_id"), "1" * 32),
        (("record", "first_crack", "time_basis"), "producer_canonical_record"),
        (("record", "session_id"), ""),
        (("record", "session_id"), "a" * 129),
        (("record", "phase"), "roasting"),
        (("record", "phase"), "dropped"),
        (("record", "bean", "acquisition_hi_seconds"), 0.5),
        (("record", "first_crack", "canonical_seconds"), -0.1),
        (("driver_read",), "not_invoked"),
        (("reason_code",), "driver_raised"),
    ],
)
def test_t2_remaining_record_contracts(path: tuple[str, ...], value: Any) -> None:
    raw = golden()
    at(raw, path[:-1])[path[-1]] = value
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize("mode", ["empty-content", "protocol-list", "typed-mirror"])
def test_t2_other_envelopes(mode: str) -> None:
    raw = golden()
    if mode == "empty-content":
        assert (
            decode_timing_evidence_reply(False, [], raw, SESSION, 1).outcome is O.MALFORMED_RESPONSE
        )
    elif mode == "protocol-list":
        raw["evidence_protocol"] = []
        assert decode(raw).outcome is O.MALFORMED_RESPONSE
    else:
        text = json.dumps(raw).replace('"evidence_version": 1', '"evidence_version": true')
        assert decode(raw, text=text).outcome is O.MALFORMED_RESPONSE


def test_t3_total_pin_validation_and_absent_bracket() -> None:
    from roastpilot_agent.timing_evidence import TimingEvidencePin

    good = decode(golden()).record
    absent = replace(good, bean=UnavailableChannel(), environment=UnavailableChannel())
    outcome, pin = admit_record(absent, None)
    assert outcome is O.EVIDENCE and pin is not None and pin.bracket is None
    assert admit_record(replace(good, evidence_sequence=2), pin)[0] is O.EVIDENCE
    pins: list[Any] = [
        {},
        internal(TimingEvidencePin)(good, []),
        internal(TimingEvidencePin)(good, (0.0,)),
        TimingEvidencePin(good, (2.0, 1.0)),
        TimingEvidencePin(good, (True, 1.0)),
        internal(TimingEvidencePin)([], None),
    ]
    for malformed in pins:
        assert admit_record(good, malformed) == (O.MALFORMED_RESPONSE, malformed)
    _, pin = admit_record(good, None)
    for field, value in (("child_epoch", 2), ("session_id", "other")):
        assert admit_record(replace(good, **{field: value}, evidence_sequence=2), pin) == (
            O.IDENTITY_REJECTED,
            pin,
        )
    pre_roast = replace(good, phase="pre_roast", charge=None, first_crack=None, evidence_sequence=2)
    assert admit_record(pre_roast, pin) == (O.EVENT_IDENTITY_REJECTED, pin)
    drop = replace(
        good.first_crack,
        kind="beans_dropped",
        source=EventSource.UNATTRIBUTED,
        time_basis=good.charge.time_basis,
        event_index=2,
        canonical_seconds=3.0,
    )
    dropped = replace(good, phase="dropped", drop=drop)
    _, drop_pin = admit_record(dropped, None)
    assert admit_record(replace(good, evidence_sequence=2), drop_pin) == (
        O.EVENT_IDENTITY_REJECTED,
        drop_pin,
    )


@pytest.mark.asyncio
async def test_t9_no_session_no_inventory_and_transport_timeout() -> None:
    empty = MCPServerProcess()
    assert await empty.discover_timing_evidence() is ToolDiscovery.ABSENT
    assert (await internal(empty)._read_timing_evidence(SESSION)).outcome is O.TRANSPORT_UNAVAILABLE
    legacy = FakeSession(reply(golden()))
    process = MCPServerProcess(session=legacy)
    assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
    assert legacy.calls == []
    session = Session(reply(golden()))
    session.waiters = []
    process = MCPServerProcess(MCPConfig(call_timeout_seconds=0.01), session=session)
    assert (
        await internal(process)._read_timing_evidence(SESSION)
    ).outcome is O.TRANSPORT_UNAVAILABLE
    assert_read_only(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("required",), []),
        (("required",), ["expected_session_id", "expected_session_id"]),
        (("type",), "string"),
        (("additionalProperties",), True),
        (("properties",), []),
        (("properties", "expected_session_id"), []),
        (("properties", "evidence_protocol"), []),
        (("properties", "expected_session_id", "type"), "integer"),
        (("properties", "expected_session_id", "minLength"), 2),
        (("properties", "expected_session_id", "maxLength"), 127),
        (("properties", "evidence_protocol", "type"), "integer"),
        (("properties", "evidence_protocol", "const"), "timing_evidence/v2"),
    ],
)
async def test_t9_schema_closed_semantics(path: tuple[str, ...], value: Any) -> None:
    raw = schema()
    at(raw, path[:-1])[path[-1]] = value
    session = Session(reply(golden()), ListToolsResult(tools=[Tool(name=TOOL, inputSchema=raw)]))
    process = MCPServerProcess(session=session)
    assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
    assert session.list_calls == 1
    assert session.calls == []


@pytest.mark.parametrize("bad", ["0" * 32 + "\n", "A" * 32])
def test_t2_coherent_invalid_epoch(bad: str) -> None:
    raw = golden()
    raw["record"]["clock"]["epoch_id"] = bad
    for slot in ("charge", "first_crack", "bean", "environment"):
        raw["record"][slot]["clock_epoch_id"] = bad
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


DROP_BADS: list[Any] = [True, [], {}, None, "1", -1, 2**53]


def dropped_golden() -> dict[str, Any]:
    raw = golden()
    raw["record"]["phase"] = "dropped"
    raw["record"]["drop"] = {
        **raw["record"]["first_crack"],
        "kind": "beans_dropped",
        "source": "unattributed",
        "time_basis": "producer_canonical_record",
        "event_index": 2,
        "canonical_seconds": 3.0,
    }
    return raw


@pytest.mark.parametrize("key", list(golden()["record"]["charge"]))
@pytest.mark.parametrize("mode", ["missing", "extra"])
def test_t2_drop_closed_shape(key: str, mode: str) -> None:
    raw = dropped_golden()
    if mode == "missing":
        del raw["record"]["drop"][key]
    else:
        raw["record"]["drop"]["extra"] = None
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("key", "bad"),
    [(key, bad) for key in ("event_index", "canonical_seconds") for bad in DROP_BADS]
    + [
        ("canonical_seconds", 1),
        ("event_index", 1.0),
        ("source", "manual_mark"),
        ("kind", "first_crack_detected"),
        ("physical_time_bound", "invented"),
        ("time_basis", "detector_clock_rebased"),
    ],
)
def test_t2_drop_exact_values(key: str, bad: Any) -> None:
    raw = dropped_golden()
    raw["record"]["drop"][key] = bad
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unavailable", "reported_none"),
        ("invalid", "non_finite"),
        ("invalid", "not_exact_float"),
    ],
)
@pytest.mark.parametrize("missing", ["status", "reason"])
def test_t2_other_channel_closed_shapes(status: str, reason: str, missing: str) -> None:
    raw = golden()
    raw["record"]["bean"] = {"status": status, "reason": reason}
    del raw["record"]["bean"][missing]
    assert decode(raw).outcome is O.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_t9_malformed_inventory_is_absent() -> None:
    session = Session(
        reply(golden()),
        ListToolsResult.model_construct(tools=[Tool.model_construct(name=TOOL, inputSchema=[])]),
    )
    process = MCPServerProcess(session=session)
    assert await process.discover_timing_evidence() is ToolDiscovery.ABSENT
    assert session.calls == []


@pytest.mark.asyncio
async def test_t5_actual_child_replacement_sends_nothing() -> None:
    adapter, process, old = await prepared()
    replacement = Session(reply(golden()))
    internal(process)._replace_session(replacement)
    assert (await adapter.read_timing_evidence()).outcome is O.NOT_ATTEMPTED
    assert_read_only(old, 0)
    assert_read_only(replacement, 0)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("protocol", O.UNSUPPORTED_PROTOCOL),
        ("session", O.IDENTITY_REJECTED),
    ],
)
def test_t2_classification_order_after_shape(change: str, expected: O) -> None:
    raw = golden()
    # Shape is coherent; a later semantic record error must not take precedence.
    raw["record"]["first_crack"]["canonical_seconds"] = 0.0
    raw["record"]["charge"]["canonical_seconds"] = 1.0
    if change == "protocol":
        raw["evidence_version"] = True
    else:
        raw["requested_session_id"] = "wrong"
    assert decode(raw).outcome is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "protocol-v2",
        "version-2",
        "version-bool",
        "protocol-bool",
        "duplicate",
        "non-finite-text",
        "signed-zero",
        "float-int-mirror",
        "oversize",
        "text-only",
        "wrapper",
        "scalar",
        "tool-error",
        "missing-record",
        "unhashable-protocol",
        "reversed-event-time",
        "unavailable-channel",
        "invalid-channel",
    ],
)
async def test_t2_adapter_envelope_and_protocol_boundary(case: str) -> None:
    raw = golden()
    text: str | None = None
    expected = O.MALFORMED_RESPONSE
    if case in ("protocol-v2", "protocol-bool", "version-2", "version-bool"):
        key = "evidence_protocol" if case.startswith("protocol") else "evidence_version"
        raw[key] = (
            True
            if case.endswith("bool")
            else ("timing_evidence/v2" if key == "evidence_protocol" else 2)
        )
        expected = O.UNSUPPORTED_PROTOCOL
    elif case == "duplicate":
        text = json.dumps(raw).replace(
            '"evidence_version": 1', '"evidence_version": 1, "evidence_version": 1'
        )
    elif case == "non-finite-text":
        text = json.dumps(raw).replace("196.00000000000003", "NaN")
    elif case == "signed-zero":
        text = json.dumps(raw).replace('"celsius": 0.0', '"celsius": -0.0')
    elif case == "float-int-mirror":
        text = json.dumps(raw).replace('"celsius": 0.0', '"celsius": 0')
    elif case == "oversize":
        text = json.dumps(raw) + " " * 16384
    elif case == "wrapper":
        raw = {"result": raw}
    elif case == "scalar":
        raw = 1
    elif case == "missing-record":
        raw["record"] = None
    elif case == "unhashable-protocol":
        raw["evidence_protocol"] = []
    elif case == "reversed-event-time":
        raw["record"]["charge"]["canonical_seconds"] = 1.0
        raw["record"]["first_crack"]["canonical_seconds"] = 0.0
    elif case in ("unavailable-channel", "invalid-channel"):
        raw["record"]["bean"] = {
            "status": "unavailable" if case == "unavailable-channel" else "invalid",
            "reason": "reported_none" if case == "unavailable-channel" else "non_finite",
        }
        expected = O.EVIDENCE
    if case == "scalar":
        envelope = CallToolResult.model_construct(
            content=[TextContent(type="text", text="1")], structuredContent=1, isError=False
        )
    else:
        envelope = reply(raw, text=text)
    if case == "text-only":
        envelope.structuredContent = None
    if case == "tool-error":
        envelope.isError = True
    adapter, _, session = await prepared()
    internal(session)._result = envelope
    result = await adapter.read_timing_evidence()
    assert result.outcome is expected
    if case == "unavailable-channel":
        assert result.record is not None
        assert isinstance(result.record.bean, UnavailableChannel)
    if case == "invalid-channel":
        assert result.record is not None
        assert isinstance(result.record.bean, InvalidChannel)
    assert_read_only(session)
