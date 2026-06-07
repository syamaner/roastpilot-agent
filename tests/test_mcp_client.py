"""E5-S1: MCP mirrors and typed tool wrappers (component plan §2, §8).

Child-process lifecycle (E5-S2) and per-tool captured fixtures (E5-S3)
extend this suite. Mirror shapes are derived from the coffee-roaster-mcp
source and validated here against the 7 Jun 2026 live-roast exports.
"""

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from roastpilot_agent.config import MCPConfig
from roastpilot_agent.mcp_client import (
    ControlCommandResult,
    EventCommandResult,
    EventSnapshot,
    ExportRoastLogResult,
    MCPConnectionError,
    MCPMirror,
    MCPServerProcess,
    MCPToolError,
    MCPToolTimeoutError,
    RoasterMCPClient,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
    parse_tool_result,
)

FIXTURES = Path(__file__).parent / "fixtures" / "live-roast-2026-06-07"

SESSION_STATE_PAYLOAD: dict[str, object] = {
    "session_id": "c570768137504d30b6a917b0cba42085",
    "active": True,
    "phase": "development",
    "created_at_utc": "2026-06-07T11:58:31.306365+00:00",
    "stopped_at_utc": None,
    "elapsed_monotonic_seconds": 1200.5,
    "heat_level_percent": 40,
    "fan_level_percent": 60,
    "cooling_on": False,
    "beans_added_at_utc": "2026-06-07T12:09:10.189739+00:00",
    "first_crack_at_utc": "2026-06-07T12:18:11.708550+00:00",
    "beans_dropped_at_utc": None,
    "cooling_started_at_utc": None,
    "cooling_stopped_at_utc": None,
    "faulted_at_utc": None,
    "beans_added_monotonic_seconds": 638.88,
    "first_crack_monotonic_seconds": 1180.4,
    "beans_dropped_monotonic_seconds": None,
    "cooling_started_monotonic_seconds": None,
    "cooling_stopped_monotonic_seconds": None,
    "faulted_monotonic_seconds": None,
    "roast_elapsed_seconds": 561.6,
    "development_time_seconds": 20.1,
    "development_percent": 3.6,
    "bean_temp_delta_60s_c": 8.0,
    "env_temp_delta_60s_c": 6.0,
    "bean_ror_c_per_min": 8.2,
    "env_ror_c_per_min": 6.4,
    "device_state": {
        "driver": "hottop_kn8828b_2k_plus",
        "connected": True,
        "bean_temp_c": 196.0,
        "env_temp_c": 214.0,
        "heat_level_percent": 40,
        "fan_level_percent": 60,
        "cooling_on": False,
        "raw_vendor_data": {"packet_age_ms": 120},
    },
    "t0_status": {
        "auto_detection_enabled": True,
        "status": "detected",
        "charge_temperature_c": 186.0,
        "current_drop_c": 30.0,
        "drop_threshold_c": 25.0,
        "detected_bean_temperature_c": 156.0,
    },
    "first_crack_status": {
        "mode": "audio",
        "status": "detected",
        "detected_at_utc": "2026-06-07T12:18:11.708550+00:00",
        "detected_monotonic_seconds": 1180.4,
        "allow_manual_override": True,
        "audio_running": True,
        "queued_window_count": 0,
        "emitted_window_count": 311,
        "dropped_window_count": 0,
        "processed_window_count": 311,
    },
    "events": [
        {
            "kind": "beans_added",
            "recorded_at_utc": "2026-06-07T12:09:10.189739+00:00",
            "monotonic_seconds": 638.88,
            "payload": {},
        }
    ],
    "log_dir": "/var/lib/roastpilot/logs/session",
}

EVENT_RESULT_PAYLOAD: dict[str, object] = {
    "session_id": "abc",
    "phase": "development",
    "event": {
        "kind": "first_crack_detected",
        "recorded_at_utc": "2026-06-07T12:18:11.708550+00:00",
        "monotonic_seconds": 1180.4,
        "payload": {"source": "audio", "confidence": 0.9066},
    },
    "event_count": 2,
}

CANNED: dict[str, object] = {
    "get_server_info": {
        "product_name": "RoastPilot",
        "package_name": "coffee-roaster-mcp",
        "version": "0.1.3",
        "transport": "stdio",
        "current_phase": "bootstrap",
        "roaster_driver": "mock",
        "first_crack_mode": "disabled",
        "bootstrap_safe": True,
        "available_bootstrap_tools": ["get_server_info", "get_runtime_config"],
        "started_at_utc": "2026-06-07T11:00:00+00:00",
    },
    "get_runtime_config": {
        "config_source": None,
        "roaster_driver": "mock",
        "roaster_port": None,
        "roaster_baudrate": 115200,
        "temperature_unit": "celsius",
        "command_interval_seconds": 0.3,
        "first_crack_mode": "disabled",
        "model_repo_id": "syamaner/coffee-first-crack-detection",
        "model_precision": "int8",
        "allow_manual_override": True,
        "log_dir": "logs",
        "sample_interval_seconds": 1.0,
        "auto_t0_detection_enabled": True,
        "auto_t0_drop_threshold_c": 25.0,
    },
    "start_roast_session": {"session": SESSION_STATE_PAYLOAD},
    "get_roast_state": SESSION_STATE_PAYLOAD,
    "set_heat": {
        "session_id": "abc",
        "phase": "development",
        "heat_level_percent": 40,
        "fan_level_percent": 60,
        "cooling_on": False,
    },
    "set_fan": {
        "session_id": "abc",
        "phase": "development",
        "heat_level_percent": 40,
        "fan_level_percent": 70,
        "cooling_on": False,
    },
    "mark_beans_added": EVENT_RESULT_PAYLOAD,
    "mark_first_crack": EVENT_RESULT_PAYLOAD,
    "drop_beans": EVENT_RESULT_PAYLOAD,
    "start_cooling": EVENT_RESULT_PAYLOAD,
    "stop_cooling": EVENT_RESULT_PAYLOAD,
    "export_roast_log": {
        "session_id": "abc",
        "log_dir": "logs/session",
        "jsonl_path": "logs/session/roast.jsonl",
        "csv_path": "logs/session/roast.csv",
        "summary_path": "logs/session/summary.json",
        "ready": True,
        "note": "export complete",
    },
    "emergency_stop": EVENT_RESULT_PAYLOAD,
}


class FakeToolCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool, arguments))
        return CANNED[tool]


@pytest.fixture
def caller() -> FakeToolCaller:
    return FakeToolCaller()


@pytest.fixture
def client(caller: FakeToolCaller) -> RoasterMCPClient:
    return RoasterMCPClient(caller)


@pytest.mark.asyncio
async def test_all_thirteen_tools_typed(client: RoasterMCPClient, caller: FakeToolCaller) -> None:
    """Every verified tool has a typed method; results validate into the
    named mirrors; the wire tool names match plan §2 exactly."""
    assert isinstance(await client.get_server_info(), ServerInfo)
    assert isinstance(await client.get_runtime_config(), RuntimeConfigSnapshot)
    assert isinstance(await client.start_roast_session(), StartRoastSessionResult)
    assert isinstance(await client.get_roast_state(), RoastSessionState)
    assert isinstance(await client.set_heat(40), ControlCommandResult)
    assert isinstance(await client.set_fan(70), ControlCommandResult)
    assert isinstance(await client.mark_beans_added(), EventCommandResult)
    assert isinstance(await client.mark_first_crack(), EventCommandResult)
    assert isinstance(await client.drop_beans(), EventCommandResult)
    assert isinstance(await client.start_cooling(), EventCommandResult)
    assert isinstance(await client.stop_cooling(), EventCommandResult)
    assert isinstance(await client.export_roast_log(), ExportRoastLogResult)
    assert isinstance(await client.emergency_stop("test"), EventCommandResult)
    assert [name for name, _ in caller.calls] == [
        "get_server_info",
        "get_runtime_config",
        "start_roast_session",
        "get_roast_state",
        "set_heat",
        "set_fan",
        "mark_beans_added",
        "mark_first_crack",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "export_roast_log",
        "emergency_stop",
    ]


@pytest.mark.asyncio
async def test_arguments_are_passed_through(
    client: RoasterMCPClient, caller: FakeToolCaller
) -> None:
    await client.set_heat(85)
    await client.get_roast_state("abc123")
    await client.emergency_stop("smoke")
    await client.get_roast_state()
    await client.export_roast_log()
    assert caller.calls == [
        ("set_heat", {"heat_level_percent": 85}),
        ("get_roast_state", {"session_id": "abc123"}),
        ("emergency_stop", {"reason": "smoke"}),
        # Defaulted optionals omit the key — never an explicit null
        # (JSON-RPC servers may treat null and absent differently).
        ("get_roast_state", {}),
        ("export_roast_log", {}),
    ]


def test_no_arbitrary_tool_surface() -> None:
    """Exactly the 13 verified tools — nothing generic, nothing extra."""
    public = {
        name
        for name in dir(RoasterMCPClient)
        if not name.startswith("_") and callable(getattr(RoasterMCPClient, name))
    }
    assert public == {
        "get_server_info",
        "get_runtime_config",
        "start_roast_session",
        "get_roast_state",
        "set_heat",
        "set_fan",
        "mark_beans_added",
        "mark_first_crack",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "export_roast_log",
        "emergency_stop",
    }


def test_session_state_mirror_round_trips() -> None:
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    assert state.phase == "development"
    assert state.device_state is not None
    assert state.device_state.driver == "hottop_kn8828b_2k_plus"
    assert state.t0_status.status == "detected"
    assert state.first_crack_status.emitted_window_count == 311
    assert state.development_percent == 3.6  # passed through, not recomputed


def test_mirrors_tolerate_new_upstream_fields() -> None:
    """extra='ignore': a new optional MCP field never breaks the agent —
    drift is the contract checker's job, not a runtime crash."""
    payload = dict(SESSION_STATE_PAYLOAD)
    payload["brand_new_field"] = "future"
    state = RoastSessionState.model_validate(payload)
    assert not hasattr(state, "brand_new_field")


# --- validation against the 7 Jun 2026 live-roast exports (real hardware) ---


def load_events(session: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in (FIXTURES / session / "roast.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record.get("type") == "event":
            rows.append(record)
    return rows


@pytest.mark.parametrize("session", ["session-1", "session-2"])
def test_live_roast_events_validate(session: str) -> None:
    events = load_events(session)
    assert events  # both sessions recorded events
    for record in events:
        snapshot = EventSnapshot.model_validate(record)
        assert snapshot.kind
        assert snapshot.recorded_at_utc


def test_manual_and_auto_t0_beans_added_payloads_both_accepted() -> None:
    """Session 1 marked beans manually (empty payload); session 2 used
    auto-T0 (source + charge/drop metadata). The mirror accepts both —
    the upstream source-marker change (plan repo f0e9502) will later make
    the manual case explicit."""
    one = next(e for e in load_events("session-1") if e["kind"] == "beans_added")
    two = next(e for e in load_events("session-2") if e["kind"] == "beans_added")
    manual = EventSnapshot.model_validate(one)
    auto = EventSnapshot.model_validate(two)
    assert manual.payload == {}
    assert auto.payload["source"] == "auto_t0"
    assert auto.payload["drop_threshold_c"] == 25.0


@pytest.mark.parametrize("session", ["session-1", "session-2"])
def test_live_roast_event_kinds_match_plan(session: str) -> None:
    """The latched singleton event kinds from plan §2 — no contract drift."""
    kinds = {e["kind"] for e in load_events(session)}
    assert kinds <= {
        "beans_added",
        "first_crack_detected",
        "beans_dropped",
        "cooling_started",
        "cooling_stopped",
        "fault",
    }


# --- E5-S2: stdio child-process transport (D6) ---


@dataclass
class FakeResult:
    structuredContent: dict[str, object] | None = None  # noqa: N815 (mirrors SDK)
    content: list[object] = field(default_factory=list[object])
    isError: bool = False  # noqa: N815


@dataclass
class FakeTextBlock:
    text: str


class FakeSession:
    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        self.calls.append((name, arguments))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class HangingSession:
    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_spawn_argv_includes_serve_positional() -> None:
    """server.json packageArguments fix: the spawn command is
    `coffee-roaster-mcp serve` — pinned per the E5-S2 criterion."""
    params = MCPServerProcess().build_server_parameters()
    assert params.command == "coffee-roaster-mcp"
    assert params.args == ["serve"]


@pytest.mark.asyncio
async def test_calls_are_timeout_bounded() -> None:
    """E4-S2 carry-forward: a hung call raises a typed timeout instead of
    stalling the tick — including emergency_stop."""
    process = MCPServerProcess(MCPConfig(call_timeout_seconds=0.05), session=HangingSession())
    with pytest.raises(MCPToolTimeoutError):
        await asyncio.wait_for(process.call_tool("emergency_stop", {}), timeout=1.0)


@pytest.mark.asyncio
async def test_dead_child_surfaces_as_typed_failure() -> None:
    """A crashed child raises MCPConnectionError — the controller's
    consecutive-failure rules map it to fail-closed; never a silent
    reconnect-and-continue."""
    process = MCPServerProcess(session=FakeSession(RuntimeError("broken pipe")))
    with pytest.raises(MCPConnectionError):
        await process.call_tool("get_roast_state", {})


@pytest.mark.asyncio
async def test_call_without_start_is_a_typed_failure() -> None:
    with pytest.raises(MCPConnectionError):
        await MCPServerProcess().call_tool("get_server_info", {})


@pytest.mark.asyncio
async def test_server_side_error_result_raises() -> None:
    result = FakeResult(isError=True, content=[FakeTextBlock("boom")])
    process = MCPServerProcess(session=FakeSession(result))
    with pytest.raises(MCPToolError):
        await process.call_tool("set_heat", {"heat_level_percent": 50})


@pytest.mark.asyncio
async def test_timeout_errors_are_connection_errors() -> None:
    """One except-clause in the controller catches every transport fault."""
    assert issubclass(MCPToolTimeoutError, MCPConnectionError)
    assert issubclass(MCPToolError, MCPConnectionError)


def test_parse_structured_dataclass_result() -> None:
    payload: dict[str, object] = {"session_id": "abc", "ready": True}
    assert parse_tool_result(FakeResult(structuredContent=payload)) == payload


def test_parse_structured_scalar_unwraps() -> None:
    assert parse_tool_result(FakeResult(structuredContent={"result": 42})) == 42


def test_parse_falls_back_to_text_json() -> None:
    result = FakeResult(content=[FakeTextBlock('{"session_id": "abc"}')])
    assert parse_tool_result(result) == {"session_id": "abc"}


def test_parse_empty_result_is_typed_failure() -> None:
    with pytest.raises(MCPConnectionError):
        parse_tool_result(FakeResult())


@pytest.mark.asyncio
async def test_transport_feeds_the_typed_client() -> None:
    """MCPServerProcess.call_tool satisfies ToolCaller: the typed client
    validates what the transport returns."""
    from typing import cast

    payload = cast("dict[str, object]", CANNED["export_roast_log"])
    result = FakeResult(structuredContent=dict(payload))
    process = MCPServerProcess(session=FakeSession(result))
    client = RoasterMCPClient(process.call_tool)
    export = await client.export_roast_log()
    assert export.ready is True


@pytest.mark.skipif(
    shutil.which("coffee-roaster-mcp") is None,
    reason="coffee-roaster-mcp not installed (E9 adds it for the vertical slice)",
)
@pytest.mark.asyncio
async def test_real_child_process_round_trip() -> None:
    """Integration: spawn the real server (bootstrap-safe mock defaults),
    health-check, one typed call, clean shutdown."""
    process = MCPServerProcess()
    await process.start()
    try:
        client = RoasterMCPClient(process.call_tool)
        info = await client.get_server_info()
        assert info.package_name == "coffee-roaster-mcp"
        assert info.bootstrap_safe is True
    finally:
        await process.stop()
    assert not process.running


# --- E5-S2 follow-up: lifecycle coverage via injected session factory ---


class FakeInitializableSession(FakeSession):
    def __init__(
        self,
        result: object,
        *,
        init_hangs: bool = False,
        init_error: Exception | None = None,
    ) -> None:
        super().__init__(result)
        self._init_hangs = init_hangs
        self._init_error = init_error
        self.initialized = False

    async def initialize(self) -> object:
        if self._init_hangs:
            await asyncio.Event().wait()
        if self._init_error is not None:
            raise self._init_error
        self.initialized = True
        return None


class FactoryProbe:
    """Session factory recording spawn params and context teardown."""

    def __init__(self, session: FakeInitializableSession) -> None:
        self.session = session
        self.params: object | None = None
        self.exited = False

    def __call__(self, params: object) -> "FactoryProbe._Context":
        self.params = params
        return FactoryProbe._Context(self)

    class _Context:
        def __init__(self, probe: "FactoryProbe") -> None:
            self._probe = probe

        async def __aenter__(self) -> FakeInitializableSession:
            return self._probe.session

        async def __aexit__(self, *exc_info: object) -> None:
            self._probe.exited = True


def info_result() -> FakeResult:
    from typing import cast

    return FakeResult(structuredContent=dict(cast("dict[str, object]", CANNED["get_server_info"])))


@pytest.mark.asyncio
async def test_start_initializes_health_checks_and_stops_cleanly() -> None:
    session = FakeInitializableSession(info_result())
    probe = FactoryProbe(session)
    process = MCPServerProcess(session_factory=probe)
    await process.start()
    assert process.running
    assert session.initialized
    assert session.calls == [("get_server_info", {})]  # health check
    params = probe.params
    assert getattr(params, "command", None) == "coffee-roaster-mcp"
    await process.stop()
    assert not process.running
    assert probe.exited  # child torn down cleanly


@pytest.mark.asyncio
async def test_start_unwinds_on_initialize_timeout() -> None:
    session = FakeInitializableSession(info_result(), init_hangs=True)
    probe = FactoryProbe(session)
    process = MCPServerProcess(MCPConfig(startup_timeout_seconds=0.05), session_factory=probe)
    with pytest.raises(MCPConnectionError):
        await asyncio.wait_for(process.start(), timeout=1.0)
    assert not process.running
    assert probe.exited  # the wedged child is not left dangling


@pytest.mark.asyncio
async def test_start_unwinds_on_failed_health_check() -> None:
    session = FakeInitializableSession(RuntimeError("server broken"))
    probe = FactoryProbe(session)
    process = MCPServerProcess(session_factory=probe)
    with pytest.raises(MCPConnectionError):
        await process.start()
    assert not process.running
    assert probe.exited


@pytest.mark.asyncio
async def test_start_with_injected_session_is_a_noop() -> None:
    process = MCPServerProcess(session=FakeSession(info_result()))
    await process.start()  # already attached: nothing to spawn
    assert process.running


@pytest.mark.asyncio
async def test_stop_without_start_is_safe() -> None:
    process = MCPServerProcess()
    await process.stop()
    assert not process.running


@pytest.mark.asyncio
async def test_malformed_text_result_is_typed_failure() -> None:
    """Review finding (E5-S2 PR): a JSONDecodeError from a malformed text
    block must surface as MCPConnectionError, not escape raw."""
    result = FakeResult(content=[FakeTextBlock("not valid json")])
    process = MCPServerProcess(session=FakeSession(result))
    with pytest.raises(MCPConnectionError):
        await process.call_tool("get_roast_state", {})


# --- E5-S3: per-tool contract fixtures (captured from the real server) ---


TOOL_RESULT_FIXTURES = Path(__file__).parent / "fixtures" / "mcp-tool-results"

#: tool fixture file → the mirror that must validate it. Re-capture via
#: scripts/capture_mcp_fixtures.py on coffee-roaster-mcp dependency bumps;
#: the mcp-contract-checker sub-agent re-derives the upstream surface and
#: diffs it against these mirrors + fixtures.
FIXTURE_MIRRORS: dict[str, type[MCPMirror]] = {
    "get_server_info": ServerInfo,
    "get_runtime_config": RuntimeConfigSnapshot,
    "start_roast_session": StartRoastSessionResult,
    "get_roast_state": RoastSessionState,
    "set_heat": ControlCommandResult,
    "set_fan": ControlCommandResult,
    "mark_beans_added": EventCommandResult,
    "mark_first_crack": EventCommandResult,
    "drop_beans": EventCommandResult,
    "start_cooling": EventCommandResult,
    "stop_cooling": EventCommandResult,
    "export_roast_log": ExportRoastLogResult,
    "emergency_stop": EventCommandResult,
}


def test_every_tool_has_a_captured_fixture() -> None:
    """One example per tool result shape (E5-S3 criterion) — exactly the
    13-tool surface, no strays."""
    captured = {path.stem for path in TOOL_RESULT_FIXTURES.glob("*.json")}
    assert captured == set(FIXTURE_MIRRORS)


@pytest.mark.parametrize("tool", sorted(FIXTURE_MIRRORS))
def test_captured_fixture_validates_into_mirror(tool: str) -> None:
    payload = json.loads((TOOL_RESULT_FIXTURES / f"{tool}.json").read_text())
    mirror = FIXTURE_MIRRORS[tool]
    instance = mirror.model_validate(payload)
    assert isinstance(instance, mirror)


def test_captured_state_is_bootstrap_safe_mock() -> None:
    """The fixtures were captured from the real server's bootstrap-safe
    defaults: mock driver, FC disabled — no hardware, no model download."""
    info = ServerInfo.model_validate(
        json.loads((TOOL_RESULT_FIXTURES / "get_server_info.json").read_text())
    )
    assert info.roaster_driver == "mock"
    assert info.first_crack_mode == "disabled"
    assert info.bootstrap_safe is True


def test_captured_emergency_stop_records_fault() -> None:
    result = EventCommandResult.model_validate(
        json.loads((TOOL_RESULT_FIXTURES / "emergency_stop.json").read_text())
    )
    assert result.event.kind == "fault"
    assert result.phase == "fault"
