"""E5-S1: MCP mirrors and typed tool wrappers (component plan §2, §8).

Child-process lifecycle (E5-S2) and per-tool captured fixtures (E5-S3)
extend this suite. Mirror shapes are derived from the coffee-roaster-mcp
source and validated here against the 7 Jun 2026 live-roast exports.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from anyio import BrokenResourceError, ClosedResourceError
from pydantic import ValidationError

from roastpilot_agent.config import DEFAULT_MCP_COMMAND, MCPConfig
from roastpilot_agent.mcp_client import (
    AmbientStatus,
    ControlCommandResult,
    EventCommandResult,
    EventSnapshot,
    ExportRoastLogResult,
    InitializableSession,
    MalformedCommandResultError,
    MCPConnectionError,
    MCPMirror,
    MCPServerProcess,
    MCPToolError,
    MCPToolTimeoutError,
    RoasterControlAdapter,
    RoasterMCPClient,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerInfo,
    SetRecordingMetadataResult,
    StartRoastSessionResult,
    ambient_reading_token,
    applied_state_from_event,
    event_backdate_seconds,
    force_terminate_process_group,
    parse_tool_result,
    project_live_ambient,
    project_mic_status,
    project_recordable_ambient,
    project_session_state,
    resolve_mcp_command,
)
from roastpilot_agent.models import AppliedRoasterState, MicHealth

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
        # coffee-roaster-mcp#190 (0.1.13): overflow diagnostics. The 7 Jun
        # fixture predates them; added so the payload exercises the current
        # mirror shape (same convention as the ambient_status block below).
        "overflow_count_last_minute": 0,
        "estimated_lost_audio_ms_last_minute": 0.0,
        "total_overflow_count": 3,
    },
    # #342 (D85): the 0.1.12 ambient triad. The 7 Jun 2026 fixture predates
    # ambient (0.1.11-era export); this key is added so the fixture validates
    # against the current (0.1.12) required RoastSessionState shape — it is not
    # part of the original live-roast capture. A hardware-representative "ok"
    # reading (matching the live validation read: 28.49 C / 38.6% / 1008.56 hPa).
    "ambient_status": {
        "mode": "yoctopuce",
        "status": "ok",
        "reason": None,
        "ambient_running": True,
        "temperature_c": 28.49,
        "humidity_percent": 38.6,
        "pressure_hpa": 1008.56,
        "last_reading_monotonic_seconds": 1200.0,
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

#: #507: drop_beans/emergency_stop event payloads always carry the driver's
#: applied heat/fan/cooling (coffee_roaster_mcp session.py
#: complete_reserved_driver_drop_snapshot / default_emergency_safety_payload) —
#: unlike the generic EVENT_RESULT_PAYLOAD above (shaped for mark_first_crack),
#: RoasterControlAdapter.drop_beans/emergency_stop parse these fields out of the
#: event, so the canned fixture for those two tools must carry them.
DROP_EVENT_RESULT_PAYLOAD: dict[str, object] = {
    "session_id": "abc",
    "phase": "dropped",
    "event": {
        "kind": "beans_dropped",
        "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
        "monotonic_seconds": 1228.9,
        "payload": {"heat_level_percent": 0, "fan_level_percent": 100, "cooling_on": True},
    },
    "event_count": 3,
}

EMERGENCY_STOP_EVENT_RESULT_PAYLOAD: dict[str, object] = {
    "session_id": "abc",
    "phase": "fault",
    "event": {
        "kind": "fault",
        "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
        "monotonic_seconds": 1228.9,
        "payload": {
            "driver": "mock",
            "driver_safety_method": "emergency_stop",
            "driver_safety_method_called": True,
            "heat_level_percent": 0,
            "fan_level_percent": 100,
            "cooling_on": True,
        },
    },
    "event_count": 3,
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
    "drop_beans": DROP_EVENT_RESULT_PAYLOAD,
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
    "emergency_stop": EMERGENCY_STOP_EVENT_RESULT_PAYLOAD,
    "set_recording_metadata": {"origin": "colombia-huila", "roast_num": 5},
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
async def test_all_fourteen_tools_typed(client: RoasterMCPClient, caller: FakeToolCaller) -> None:
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
    assert isinstance(
        await client.set_recording_metadata("colombia-huila", 5),
        SetRecordingMetadataResult,
    )
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
        "set_recording_metadata",
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
    """Exactly the 14 verified tools — nothing generic, nothing extra."""
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
        "set_recording_metadata",
    }


def test_session_state_mirror_round_trips() -> None:
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    assert state.phase == "development"
    assert state.device_state is not None
    assert state.device_state.driver == "hottop_kn8828b_2k_plus"
    assert state.t0_status.status == "detected"
    assert state.first_crack_status.emitted_window_count == 311
    assert state.development_percent == 3.6  # passed through, not recomputed
    # #342 (D85): the ambient triad mirrors the MCP's 0.1.12 wire shape byte-for-byte.
    assert state.ambient_status.mode == "yoctopuce"
    assert state.ambient_status.status == "ok"
    assert state.ambient_status.temperature_c == 28.49
    assert state.ambient_status.humidity_percent == 38.6
    assert state.ambient_status.pressure_hpa == 1008.56


def test_ambient_status_mirror_round_trips_unavailable() -> None:
    """#342: an unavailable/disabled MCP ambient config round-trips with every
    numeric field null and a human-readable ``reason`` — the fail-soft contract."""
    payload = _state_payload(
        0.0,
        ambient_status={
            "mode": "disabled",
            "status": "disabled",
            "reason": "Ambient sensing is disabled by configuration.",
            "ambient_running": False,
            "temperature_c": None,
            "humidity_percent": None,
            "pressure_hpa": None,
            "last_reading_monotonic_seconds": None,
        },
    )
    state = RoastSessionState.model_validate(payload)
    assert state.ambient_status.mode == "disabled"
    assert state.ambient_status.status == "disabled"
    assert state.ambient_status.reason == "Ambient sensing is disabled by configuration."
    assert state.ambient_status.temperature_c is None
    assert state.ambient_status.humidity_percent is None
    assert state.ambient_status.pressure_hpa is None


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
    """server.json packageArguments fix: the spawn args are
    `<resolved-command> serve` — pinned per the E5-S2 criterion. The command
    itself is the default, resolved to the in-venv console script
    (test_default_command_resolves_to_in_venv_script covers that resolution)."""
    params = MCPServerProcess().build_server_parameters()
    assert params.command == resolve_mcp_command(DEFAULT_MCP_COMMAND)
    assert params.args == ["serve"]


def test_default_command_resolves_to_in_venv_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spawn-hardening (homebrew-stale-deps segfault): when the command is left
    at the default and a console script exists beside ``sys.executable``, the
    spawn uses that ABSOLUTE in-venv path — never a bare-PATH lookup that could
    pick up a foreign install."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    interpreter = fake_bin / "python"
    interpreter.write_text("")  # the running interpreter's location
    script = fake_bin / DEFAULT_MCP_COMMAND
    script.write_text("")  # the in-venv console script next to it
    monkeypatch.setattr("roastpilot_agent.mcp_client.sys.executable", str(interpreter))
    # A PATH entry holding a DIFFERENT (foreign) install that must NOT win.
    foreign = tmp_path / "homebrew"
    foreign.mkdir()
    (foreign / DEFAULT_MCP_COMMAND).write_text("")
    monkeypatch.setenv("PATH", str(foreign))

    assert resolve_mcp_command(DEFAULT_MCP_COMMAND) == str(script)
    assert MCPServerProcess().build_server_parameters().command == str(script)


def test_explicit_command_is_used_verbatim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit operator/config override always wins, unchanged — even when
    an in-venv default script exists beside ``sys.executable``. The operator's
    deliberate binary choice is never second-guessed."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    interpreter = fake_bin / "python"
    interpreter.write_text("")
    (fake_bin / DEFAULT_MCP_COMMAND).write_text("")  # default script present...
    monkeypatch.setattr("roastpilot_agent.mcp_client.sys.executable", str(interpreter))

    override = "/opt/homebrew/bin/coffee-roaster-mcp"
    assert resolve_mcp_command(override) == override  # ...but the override wins
    params = MCPServerProcess(MCPConfig(command=override)).build_server_parameters()
    assert params.command == override


def test_default_command_falls_back_to_path_then_bare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no in-venv script beside the interpreter, the default resolves via a
    PATH lookup; with nothing on PATH either, it falls back to the bare name and
    lets the transport's own spawn report the failure."""
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    interpreter = empty_bin / "python"
    interpreter.write_text("")  # no console script beside it
    monkeypatch.setattr("roastpilot_agent.mcp_client.sys.executable", str(interpreter))

    path_dir = tmp_path / "onpath"
    path_dir.mkdir()
    on_path = path_dir / DEFAULT_MCP_COMMAND
    on_path.write_text("")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))
    assert resolve_mcp_command(DEFAULT_MCP_COMMAND) == str(on_path)

    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert resolve_mcp_command(DEFAULT_MCP_COMMAND) == DEFAULT_MCP_COMMAND


def test_spawn_env_is_none_without_overrides() -> None:
    """No config env → inherit the transport's default safe environment."""
    assert MCPServerProcess().build_server_parameters().env is None


def test_spawn_env_merges_overrides_over_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config env overrides are merged over the agent's own environment (so the
    child keeps PATH) — E9-S2 selects the mock driver this way."""
    monkeypatch.setenv("PATH", "/usr/bin")
    process = MCPServerProcess(MCPConfig(env={"COFFEE_ROASTER_DRIVER": "mock"}))
    env = process.build_server_parameters().env
    assert env is not None
    assert env["COFFEE_ROASTER_DRIVER"] == "mock"
    assert env["PATH"] == "/usr/bin"  # inherited, not dropped


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
    not os.path.isfile(resolve_mcp_command(DEFAULT_MCP_COMMAND)),
    reason=(
        "coffee-roaster-mcp not installed where the spawn resolves it "
        "(resolve_mcp_command prefers the in-venv console script, then PATH; "
        "E9 adds it for the vertical slice)"
    ),
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
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    await process.start()
    assert process.running
    assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
    assert session.initialized
    assert session.calls == [("get_server_info", {})]  # health check
    params = probe.params
    # The default command is resolved to the in-venv console script before
    # spawning (spawn-hardening); the bare-name case is covered by
    # test_default_command_falls_back_to_path_then_bare.
    assert getattr(params, "command", None) == resolve_mcp_command(DEFAULT_MCP_COMMAND)
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


class _OwnerAbort(BaseException):
    """A non-``Exception`` used to model an owner-task abort before ready.

    A plain ``BaseException`` subclass (not ``KeyboardInterrupt``, which pytest
    hijacks) slips past ``_run_session``'s inner ``except Exception`` and thus
    past ``start()``'s ``except Exception`` — exercising the #484 backstop that
    resolves ``ready`` and reaps the owner so ``start()`` never hangs.
    """


class _BaseExceptionAtEnterFactory:
    """A session factory whose context ``__aenter__`` raises a BaseException."""

    class _Context:
        async def __aenter__(self) -> FakeInitializableSession:
            raise _OwnerAbort("spawn aborted before ready")

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    def __call__(self, params: object) -> "_BaseExceptionAtEnterFactory._Context":
        return _BaseExceptionAtEnterFactory._Context()


@pytest.mark.asyncio
async def test_start_does_not_hang_when_owner_dies_before_ready() -> None:
    """#484 Low-1: an owner task that exits without the normal startup-failure
    path must still resolve ``ready`` — ``start()`` fails closed within the bound
    rather than hanging forever, the owner is reaped, and the process is left
    not-running.

    The ``_run_session`` backstop resolves ``ready`` for ANY exit path; a
    non-``Exception`` abort (like this ``_OwnerAbort``) is normalised to a clean
    ``MCPConnectionError`` so the operator gets a fail-closed startup error, not a
    raw ``BaseException`` or a hang.
    """
    process = MCPServerProcess(session_factory=_BaseExceptionAtEnterFactory())
    # Bound the whole call so a regression to an unbounded await fails loudly (a
    # wait_for TimeoutError), not hangs — proving start() actually returned.
    with pytest.raises(MCPConnectionError):
        await asyncio.wait_for(process.start(), timeout=1.0)
    assert not process.running
    # The owner task was reaped, not orphaned (no leftover per-spawn state).
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    assert process._stop_requested is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_start_bounds_an_owner_that_never_reports_ready() -> None:
    """#484 Low-1: the outer ``await ready`` is bounded, so an owner wedged
    before it can report readiness becomes a clean startup failure, not a hang."""
    session = FakeInitializableSession(info_result(), init_hangs=True)
    probe = FactoryProbe(session)
    # Tiny startup timeout so the inner initialize() bound (and thus the outer
    # ready bound = startup + margin) trips fast; the outer wait_for guards
    # against a hang if the inner bound were ever removed.
    process = MCPServerProcess(MCPConfig(startup_timeout_seconds=0.05), session_factory=probe)
    with pytest.raises(MCPConnectionError):
        await asyncio.wait_for(process.start(), timeout=1.0)
    assert not process.running


class _BlockingEnterFactory:
    """A factory whose context ``__aenter__`` blocks forever, so ``start()``
    parks on ``await ready`` — lets a test cancel ``start()`` mid-flight."""

    def __init__(self) -> None:
        self.exited = False

    def __call__(self, params: object) -> "_BlockingEnterFactory._Context":
        return _BlockingEnterFactory._Context(self)

    class _Context:
        def __init__(self, probe: "_BlockingEnterFactory") -> None:
            self._probe = probe

        async def __aenter__(self) -> FakeInitializableSession:
            await asyncio.Event().wait()  # never returns
            raise AssertionError("unreachable")  # pragma: no cover

        async def __aexit__(self, *exc_info: object) -> None:
            self._probe.exited = True


@pytest.mark.asyncio
async def test_start_cancelled_mid_flight_reaps_the_owner() -> None:
    """#484 Low-1: cancelling ``start()`` while it awaits ``ready`` (e.g. Ctrl-C
    during a slow spawn) must reap the owner task and clear per-spawn state, not
    orphan it. Exercises ``start()``'s ``except BaseException`` reap path."""
    factory = _BlockingEnterFactory()
    process = MCPServerProcess(session_factory=factory)
    start_task = asyncio.create_task(process.start())
    # Let start() reach its `await ready` (the owner is blocked in __aenter__).
    await asyncio.sleep(0.05)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task
    # The owner was reaped and per-spawn state cleared — nothing orphaned.
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    assert process._stop_requested is None  # pyright: ignore[reportPrivateUsage]
    assert not process.running


@pytest.mark.asyncio
async def test_start_cancellation_retains_owner_that_delays_cancel() -> None:
    """A cancellation-resistant startup owner blocks replacement until stop."""
    release = asyncio.Event()
    force_terminate_calls: list[int] = []

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    class CancellationResistantEnter:
        async def __aenter__(self) -> FakeInitializableSession:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=lambda _params: CancellationResistantEnter(),
        force_terminate=force_terminate,
    )
    start_task = asyncio.create_task(process.start())
    await asyncio.sleep(0.01)
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    start_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=0.5)
        assert not owner.done()
        assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
        assert process.stop_unconfirmed is True
        assert force_terminate_calls == [1]
        with pytest.raises(MCPConnectionError, match="still tearing down"):
            await process.start()
    finally:
        release.set()
        done, _pending = await asyncio.wait({owner}, timeout=0.5)
        assert owner in done
        assert owner.cancelled()

    await process.stop()
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    assert force_terminate_calls == [1]


class _SlowExitOnHealthFailFactory:
    """A factory whose ``__aenter__`` SUCCEEDS but the health check fails, and
    whose ``__aexit__`` is SLOW (models an in-task child cleanup mid-unwind).

    The session's ``get_server_info`` raises → ``_run_session`` unwinds its
    ``async with`` (running this slow ``__aexit__`` IN THE OWNER TASK) after
    resolving ``ready`` exceptionally. The reap must let that exit COMPLETE, not
    cancel it mid-way (Codex #492-1). ``exit_completed`` records that it did."""

    def __init__(self, exit_delay: float) -> None:
        self._exit_delay = exit_delay
        self.exit_started = False
        self.exit_completed = False

    def __call__(self, params: object) -> "_SlowExitOnHealthFailFactory._Context":
        return _SlowExitOnHealthFailFactory._Context(self)

    class _Context:
        def __init__(self, probe: "_SlowExitOnHealthFailFactory") -> None:
            self._probe = probe

        async def __aenter__(self) -> FakeInitializableSession:
            # health check (get_server_info) raises → startup failure after enter.
            return FakeInitializableSession(RuntimeError("health check fails"))

        async def __aexit__(self, *exc_info: object) -> None:
            self._probe.exit_started = True
            await asyncio.sleep(self._probe._exit_delay)  # in-task cleanup
            self._probe.exit_completed = True


@pytest.mark.asyncio
async def test_start_failure_lets_owner_teardown_complete_not_cancelled() -> None:
    """#484 Codex-P1: when startup fails AFTER the context is entered, the owner
    is mid-teardown (aclose running in its own task). The reap must AWAIT its
    natural completion, not cancel it — cancelling mid-unwind could abort the
    in-task child cleanup and orphan the child.

    A slow ``__aexit__`` (0.1 s) models that cleanup; the reap's
    ``stop_timeout_seconds`` bound is comfortably larger, so the exit must run to
    completion. ``start()`` still raises a clean ``MCPConnectionError``."""
    factory = _SlowExitOnHealthFailFactory(exit_delay=0.1)
    # stop_timeout_seconds (the reap bound) >> exit_delay so natural completion wins.
    process = MCPServerProcess(MCPConfig(stop_timeout_seconds=5.0), session_factory=factory)
    with pytest.raises(MCPConnectionError):
        await asyncio.wait_for(process.start(), timeout=2.0)
    assert factory.exit_started
    # The load-bearing assertion: the owner's teardown COMPLETED (was not
    # cancelled mid-unwind by the reap).
    assert factory.exit_completed, "owner teardown was cancelled mid-unwind (orphan risk)"
    assert not process.running
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    # A clean startup failure never force-terminates (the teardown completed).
    assert process.stop_unconfirmed is False


@pytest.mark.asyncio
async def test_start_failure_reap_tolerates_concurrent_stop_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent stop may clear a naturally completed failed owner."""
    real_wait = asyncio.wait
    reap_observed_completion = asyncio.Event()
    allow_reap_to_finish = asyncio.Event()
    pause_next_wait = True

    async def pausing_wait(
        tasks: set[asyncio.Task[None]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal pause_next_wait
        result = await real_wait(tasks, timeout=timeout)
        if pause_next_wait:
            pause_next_wait = False
            reap_observed_completion.set()
            await allow_reap_to_finish.wait()
        return result

    monkeypatch.setattr(asyncio, "wait", pausing_wait)
    process = MCPServerProcess(
        session_factory=FactoryProbe(FakeInitializableSession(RuntimeError("server broken")))
    )
    start_task = asyncio.create_task(process.start())
    try:
        await asyncio.wait_for(reap_observed_completion.wait(), timeout=0.5)
        owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
        assert owner is not None
        assert owner.done()

        await process.stop()
        assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]

        allow_reap_to_finish.set()
        with pytest.raises(MCPConnectionError):
            await asyncio.wait_for(start_task, timeout=0.5)
    finally:
        monkeypatch.setattr(asyncio, "wait", real_wait)
        allow_reap_to_finish.set()
        if not start_task.done():
            start_task.cancel()
        with suppress(BaseException):
            await asyncio.wait_for(start_task, timeout=0.5)


@pytest.mark.asyncio
async def test_start_failure_reap_tolerates_concurrent_cancel_and_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent owner cancellation/finalization cannot duplicate fail-close."""
    real_wait = asyncio.wait
    reap_observed_pending = asyncio.Event()
    allow_reap_to_finish = asyncio.Event()
    force_terminate_calls: list[int] = []
    pause_next_wait = True

    class PendingFailureExit:
        async def __aenter__(self) -> FakeInitializableSession:
            return FakeInitializableSession(RuntimeError("server broken"))

        async def __aexit__(self, *exc_info: object) -> None:
            await asyncio.Event().wait()

    async def pausing_wait(
        tasks: set[asyncio.Task[None]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal pause_next_wait
        result = await real_wait(tasks, timeout=timeout)
        if pause_next_wait:
            pause_next_wait = False
            assert not result[0]
            reap_observed_pending.set()
            await allow_reap_to_finish.wait()
        return result

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    monkeypatch.setattr(asyncio, "wait", pausing_wait)
    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.01),
        session_factory=lambda _params: PendingFailureExit(),
        force_terminate=force_terminate,
    )
    start_task = asyncio.create_task(process.start())
    try:
        await asyncio.wait_for(reap_observed_pending.wait(), timeout=0.5)
        owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
        assert owner is not None
        assert not owner.done()

        owner.cancel()
        done, _pending = await real_wait({owner}, timeout=0.5)
        assert owner in done
        await process.stop()
        assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]

        allow_reap_to_finish.set()
        with pytest.raises(MCPConnectionError):
            await asyncio.wait_for(start_task, timeout=0.5)
        assert force_terminate_calls == [1]
    finally:
        monkeypatch.setattr(asyncio, "wait", real_wait)
        allow_reap_to_finish.set()
        if not start_task.done():
            start_task.cancel()
        with suppress(BaseException):
            await asyncio.wait_for(start_task, timeout=0.5)


@pytest.mark.asyncio
async def test_ready_timeout_bound_includes_call_timeout() -> None:
    """#484 Codex-P2: the ``await ready`` bound must cover BOTH inner bounds the
    owner composes before resolving ready — ``initialize()`` (startup_timeout) and
    ``get_server_info`` (call_timeout) run in sequence, so the bound is at least
    their sum. Bounding on startup_timeout alone would false-fail a large
    call_timeout deployment."""
    process = MCPServerProcess(MCPConfig(startup_timeout_seconds=15.0, call_timeout_seconds=30.0))
    bound = process._ready_timeout_seconds()  # pyright: ignore[reportPrivateUsage]
    # The bound must cover BOTH inner bounds (their sum), plus a non-negative margin.
    assert bound >= 15.0 + 30.0

    # Raising ONLY call_timeout must widen the bound by exactly that delta — proof
    # the call timeout is a term of the sum, the regression Codex flagged.
    wider = MCPServerProcess(MCPConfig(startup_timeout_seconds=15.0, call_timeout_seconds=120.0))
    wider_bound = wider._ready_timeout_seconds()  # pyright: ignore[reportPrivateUsage]
    assert wider_bound - bound == pytest.approx(120.0 - 30.0)


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
    assert process.stop_unconfirmed is False


@pytest.mark.asyncio
async def test_clean_stop_does_not_force_terminate() -> None:
    """A graceful stop within the bound leaves stop_unconfirmed False and
    never invokes the force-terminate hook (#212)."""
    calls: list[int] = []

    def force_terminate() -> bool:
        calls.append(1)
        return True

    session = FakeInitializableSession(info_result())
    probe = FactoryProbe(session)
    process = MCPServerProcess(session_factory=probe, force_terminate=force_terminate)
    await process.start()
    await process.stop()
    assert not process.running
    assert probe.exited  # graceful teardown ran
    assert process.stop_unconfirmed is False
    assert calls == []  # force-terminate never reached


@pytest.mark.asyncio
async def test_stop_with_owner_but_no_stop_event_still_reaps() -> None:
    """Defensive-arm coverage (#492 codecov): ``stop()`` guards ``_stop_requested``
    with ``if stop_requested is not None`` before signalling. ``start()`` always
    sets both the owner task and the event together, so the ``is None`` arm is
    unreachable in normal flow — but it must still reap the owner cleanly if some
    future path ever leaves the event unset. Drive the arm directly: an owner task
    that is already complete + ``_stop_requested = None`` → ``stop()`` skips the
    ``.set()``, awaits the (done) owner, and returns a clean, cleared state."""

    async def _finished_owner() -> None:
        return None

    process = MCPServerProcess()
    owner = asyncio.create_task(_finished_owner())
    await owner  # ensure it is DONE so the reap await returns immediately
    process._owner_task = owner  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = None  # pyright: ignore[reportPrivateUsage] — the arm under test

    await process.stop()

    # Clean reap: no force-terminate, not unconfirmed, per-spawn state cleared.
    assert process.stop_unconfirmed is False
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    assert not process.running


class WedgedContext:
    """A session context whose teardown blocks forever — models a wedged MCP
    child whose graceful ``aclose`` never returns (#212)."""

    def __init__(self, probe: "WedgedFactoryProbe") -> None:
        self._probe = probe

    async def __aenter__(self) -> FakeInitializableSession:
        return self._probe.session

    async def __aexit__(self, *exc_info: object) -> None:
        self._probe.exited = True
        await asyncio.sleep(100)  # never returns within the test


class WedgedFactoryProbe:
    """A session factory that hands back a :class:`WedgedContext`."""

    def __init__(self, session: FakeInitializableSession) -> None:
        self.session = session
        self.params: object | None = None
        self.exited = False

    def __call__(self, params: object) -> WedgedContext:
        self.params = params
        return WedgedContext(self)


@pytest.mark.asyncio
async def test_stop_bounds_a_wedged_child_and_force_terminates() -> None:
    """The headline #212 guarantee: a wedged-child teardown does not hang
    stop() past stop_timeout_seconds; it force-terminates exactly once, sets
    stop_unconfirmed, and returns cleanly so the agent can always exit."""
    calls: list[int] = []

    def force_terminate() -> bool:
        calls.append(1)
        return True

    session = FakeInitializableSession(info_result())
    probe = WedgedFactoryProbe(session)
    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=probe,
        force_terminate=force_terminate,
    )
    await process.start()
    # Outer guard: if stop() failed to bound the wedged aclose, this raises
    # rather than hanging the suite — proving the bound, not just observing it.
    await asyncio.wait_for(process.stop(), timeout=1.0)
    assert process.stop_unconfirmed is True
    assert calls == [1]  # force-terminate invoked exactly once
    assert not process.running  # state cleared even on the timeout path


class RaisingExitContext:
    """A session context whose ``aclose`` RAISES — models a broken-pipe teardown
    after a child segfault (roast 2): ``stack.aclose()`` re-raises
    ``BrokenResourceError`` / ``ClosedResourceError`` / ``RuntimeError`` rather
    than returning cleanly. A NORMAL event on this rig, not an unreachable one."""

    def __init__(self, probe: "RaisingExitFactoryProbe") -> None:
        self._probe = probe

    async def __aenter__(self) -> FakeInitializableSession:
        return self._probe.session

    async def __aexit__(self, *exc_info: object) -> None:
        self._probe.exited = True
        raise self._probe.exit_error


class RaisingExitFactoryProbe:
    """A session factory that hands back a :class:`RaisingExitContext`."""

    def __init__(self, session: FakeInitializableSession, exit_error: BaseException) -> None:
        self.session = session
        self.exit_error = exit_error
        self.exited = False

    def __call__(self, params: object) -> RaisingExitContext:
        return RaisingExitContext(self)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_error",
    [
        pytest.param(BrokenResourceError(), id="broken-resource"),
        pytest.param(ClosedResourceError(), id="closed-resource"),
        pytest.param(RuntimeError("aclose blew up"), id="runtime-error"),
    ],
)
async def test_stop_fails_closed_when_aclose_raises(
    exit_error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    """#484 MEDIUM: a RAISING ``aclose`` (broken pipes after a child segfault) is
    an UNCONFIRMED stop, not a clean one — stop() must fail closed exactly like
    the wedged-child timeout: force-terminate the child group, set
    ``stop_unconfirmed = True``, log the error, and still return without raising.

    Recording it as a confirmed clean stop (the old ``except Exception: log``
    behaviour) would let a subsequent respawn sail past the #431 unconfirmed-stop
    guard and let a restart skip ``operator_recovery_required``."""
    calls: list[int] = []

    def force_terminate() -> bool:
        calls.append(1)
        return True

    session = FakeInitializableSession(info_result())
    probe = RaisingExitFactoryProbe(session, exit_error)
    process = MCPServerProcess(session_factory=probe, force_terminate=force_terminate)
    await process.start()
    with caplog.at_level(logging.ERROR, logger="roastpilot_agent.mcp_client"):
        # Bound so a regression that re-raises would fail loudly, not hang.
        await asyncio.wait_for(process.stop(), timeout=1.0)
    assert probe.exited  # the raising aclose actually ran
    # Fail closed: unconfirmed + force-terminated exactly once.
    assert process.stop_unconfirmed is True
    assert calls == [1]
    assert not process.running
    # The teardown error is logged for post-roast diagnosis.
    assert any("stop raised during teardown" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stop_cancelled_mid_wait_fails_closed_and_reraises() -> None:
    """#484 Codex-P3: if stop()'s OWN task is cancelled during the shielded wait,
    the CancelledError (a BaseException) must NOT silently wipe state with the
    child possibly alive. stop() must fail closed first — force-terminate +
    ``stop_unconfirmed = True`` — and then RE-RAISE the cancellation (mark then
    propagate; a cancellation is never swallowed)."""
    calls: list[int] = []

    def force_terminate() -> bool:
        calls.append(1)
        return True

    session = FakeInitializableSession(info_result())
    # A wedged __aexit__ (sleeps 100 s) keeps stop() parked on the shielded wait
    # long enough for the test to cancel it mid-wait.
    probe = WedgedFactoryProbe(session)
    process = MCPServerProcess(session_factory=probe, force_terminate=force_terminate)
    await process.start()
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None

    stop_task: asyncio.Task[None] | None = None
    try:
        stop_task = asyncio.create_task(process.stop())
        # Let stop() reach its shielded `await` (the owner is wedged in __aexit__).
        await asyncio.sleep(0.05)
        with pytest.raises(MCPConnectionError, match="still tearing down"):
            await process.start()
        stop_task.cancel()
        # The cancellation must PROPAGATE to the caller — never be swallowed.
        with pytest.raises(asyncio.CancelledError):
            await stop_task

        # ...but first it failed closed: force-terminate fired, unconfirmed marked.
        assert calls == [1], "cancelled stop() did not force-terminate the child"
        assert process.stop_unconfirmed is True
        assert not process.running
        assert owner.done(), "cancelled stop() leaked its MCP owner task"
    finally:
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
        if stop_task is not None:
            with suppress(BaseException):
                await asyncio.wait_for(stop_task, timeout=0.5)
        if not owner.done():
            owner.cancel()
            with suppress(BaseException):
                await asyncio.wait_for(owner, timeout=0.5)
        await process.stop()


@pytest.mark.asyncio
async def test_stop_cancelled_after_clean_owner_completion_does_not_force_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation cannot signal a completed owner's stale PID hook."""
    real_wait = asyncio.wait
    owner_completion_observed = asyncio.Event()
    allow_wait_to_return = asyncio.Event()
    force_terminate_calls: list[int] = []
    pause_next_wait = True

    async def pausing_wait(
        tasks: set[asyncio.Task[None]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal pause_next_wait
        result = await real_wait(tasks, timeout=timeout)
        if pause_next_wait:
            pause_next_wait = False
            assert result[0]
            owner_completion_observed.set()
            await allow_wait_to_return.wait()
        return result

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    monkeypatch.setattr(asyncio, "wait", pausing_wait)
    process = MCPServerProcess(
        session_factory=FactoryProbe(FakeInitializableSession(info_result())),
        force_terminate=force_terminate,
    )
    await process.start()
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    stop_task = asyncio.create_task(process.stop())
    try:
        await asyncio.wait_for(owner_completion_observed.wait(), timeout=0.5)
        assert owner.done()
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=0.5)
        assert force_terminate_calls == []
        assert process.stop_unconfirmed is False
        assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    finally:
        monkeypatch.setattr(asyncio, "wait", real_wait)
        allow_wait_to_return.set()
        if not stop_task.done():
            stop_task.cancel()
        with suppress(BaseException):
            await asyncio.wait_for(stop_task, timeout=0.5)
        if process._owner_task is not None:  # pyright: ignore[reportPrivateUsage]
            await process.stop()


@pytest.mark.asyncio
async def test_stop_cancellation_reap_stays_bounded_when_owner_delays_cancel() -> None:
    """A cancellation-resistant owner cannot overrun stop's teardown bound."""
    release = asyncio.Event()
    force_terminate_calls: list[int] = []

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    class CancellationResistantContext:
        async def __aenter__(self) -> FakeInitializableSession:
            return FakeInitializableSession(info_result())

        async def __aexit__(self, *exc_info: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

    clean_probe = FactoryProbe(FakeInitializableSession(info_result()))
    spawn_count = 0

    def session_factory(
        params: object,
    ) -> AbstractAsyncContextManager[InitializableSession]:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            return CancellationResistantContext()
        return clean_probe(params)

    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=session_factory,
        force_terminate=force_terminate,
    )
    await process.start()
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    stop_task = asyncio.create_task(process.stop())
    await asyncio.sleep(0.01)
    stop_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=0.5)
        assert not owner.done()
        assert process.stop_unconfirmed is True
        assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(MCPConnectionError, match="still tearing down"):
            await process.start()
        await process.stop()
        assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
        assert force_terminate_calls == [1]
        retry_stop = asyncio.create_task(process.stop())
        await asyncio.sleep(0.01)
        retry_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry_stop
        assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
        assert force_terminate_calls == [1]
    finally:
        release.set()
        done, _pending = await asyncio.wait({owner}, timeout=0.5)
        assert owner in done
        assert owner.cancelled()

    await process.stop()
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
    assert force_terminate_calls == [1]
    incident_id = process.teardown_incident_id
    assert incident_id is not None
    with pytest.raises(MCPConnectionError, match="hardware-clear acknowledgement"):
        await process.start()
    process.acknowledge_hardware_clear(incident_id)
    await process.start()
    replacement_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert replacement_owner is not None
    assert replacement_owner is not owner
    await process.stop()
    await asyncio.wait_for(replacement_owner, timeout=0.5)
    assert replacement_owner.done()
    await process.stop()
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_start_refuses_completed_owner_until_stop_finalizes_it() -> None:
    """Only stop may drain a completed retained owner before respawn."""
    process = MCPServerProcess(
        session_factory=FactoryProbe(FakeInitializableSession(info_result()))
    )

    async def cancelled_owner() -> None:
        raise asyncio.CancelledError

    previous_owner = asyncio.create_task(cancelled_owner())
    done, _pending = await asyncio.wait({previous_owner}, timeout=0.5)
    assert previous_owner in done
    assert previous_owner.cancelled()
    process._owner_task = previous_owner  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = asyncio.Event()  # pyright: ignore[reportPrivateUsage]
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MCPConnectionError, match="awaiting stop finalization"):
        await process.start()
    assert process._owner_task is previous_owner  # pyright: ignore[reportPrivateUsage]
    await process.stop()
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MCPConnectionError, match="hardware-clear acknowledgement"):
        await process.start()
    process.acknowledge_hardware_clear("a" * 32)
    await process.start()
    replacement_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert replacement_owner is not None
    assert replacement_owner is not previous_owner
    assert process.stop_unconfirmed is False
    await process.stop()


@pytest.mark.asyncio
async def test_hardware_clear_acknowledgement_permits_one_fresh_spawn() -> None:
    """A completed uncertain generation is cleared without any MCP write."""
    session = FakeInitializableSession(info_result())
    probe = FactoryProbe(session)
    process = MCPServerProcess(session_factory=probe)

    async def cancelled_owner() -> None:
        raise asyncio.CancelledError

    previous_owner = asyncio.create_task(cancelled_owner())
    done, _pending = await asyncio.wait({previous_owner}, timeout=0.5)
    assert previous_owner in done
    process._owner_task = previous_owner  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = asyncio.Event()  # pyright: ignore[reportPrivateUsage]
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]

    process.acknowledge_hardware_clear("a" * 32)
    assert process.stop_unconfirmed is False
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]

    await process.start()
    replacement_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert replacement_owner is not None
    await process.start()
    assert process._owner_task is replacement_owner  # pyright: ignore[reportPrivateUsage]
    assert session.calls == [("get_server_info", {})]
    await process.stop()

    with pytest.raises(MCPConnectionError, match="no unconfirmed"):
        process.acknowledge_hardware_clear("a" * 32)


@pytest.mark.asyncio
async def test_hardware_clear_acknowledgement_rejects_owner_in_flight() -> None:
    """Physical confirmation cannot erase an owner that is still unwinding."""
    release = asyncio.Event()

    async def running_owner() -> None:
        await release.wait()

    owner = asyncio.create_task(running_owner())
    process = MCPServerProcess()
    process._owner_task = owner  # pyright: ignore[reportPrivateUsage]
    process._stop_requested = asyncio.Event()  # pyright: ignore[reportPrivateUsage]
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(MCPConnectionError, match="still tearing down"):
            process.acknowledge_hardware_clear("a" * 32)
        assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]
        assert process.stop_unconfirmed is True
    finally:
        release.set()
        await owner


def test_hardware_clear_acknowledgement_rejects_attached_session() -> None:
    """Physical confirmation cannot discard a still-attached MCP session."""
    session = FakeInitializableSession(info_result())
    process = MCPServerProcess(session=session)
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MCPConnectionError, match="session is still attached"):
        process.acknowledge_hardware_clear("a" * 32)

    assert process.running is True
    assert process.stop_unconfirmed is True
    assert process.teardown_incident_id == "a" * 32


def test_hardware_clear_acknowledgement_requires_incident_identity() -> None:
    """Legacy/corrupt uncertain state without an incident remains blocked."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MCPConnectionError, match="no acknowledgement identity"):
        process.acknowledge_hardware_clear("a" * 32)

    assert process.stop_unconfirmed is True


def test_hardware_clear_acknowledgement_removes_rendered_generation_config(
    tmp_path: Path,
) -> None:
    """Acknowledgement discards the uncertain generation's rendered config."""
    rendered = tmp_path / "rendered-config"
    rendered.mkdir()
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    process._rendered_yaml_dir = rendered  # pyright: ignore[reportPrivateUsage]

    process.acknowledge_hardware_clear("a" * 32)

    assert not rendered.exists()
    assert process._rendered_yaml_dir is None  # pyright: ignore[reportPrivateUsage]


def test_unconfirmed_teardown_mints_a_distinct_incident_per_lifecycle() -> None:
    """A delayed acknowledgement token cannot name a later teardown incident."""
    process = MCPServerProcess(force_terminate=lambda: True)

    process._fail_closed_teardown("incident A")  # pyright: ignore[reportPrivateUsage]
    first = process.teardown_incident_id
    assert first is not None
    assert len(first) == 32
    process._fail_closed_teardown("same incident")  # pyright: ignore[reportPrivateUsage]
    assert process.teardown_incident_id == first

    process.acknowledge_hardware_clear(first)
    process._fail_closed_teardown("incident B")  # pyright: ignore[reportPrivateUsage]
    second = process.teardown_incident_id
    assert second is not None
    assert second != first


@pytest.mark.asyncio
async def test_stop_fails_closed_for_a_newly_cancelled_owner() -> None:
    """An owner cancelled outside stop is newly unconfirmed and force-killed."""
    calls: list[int] = []

    def force_terminate() -> bool:
        calls.append(1)
        return True

    process = MCPServerProcess(
        session_factory=FactoryProbe(FakeInitializableSession(info_result())),
        force_terminate=force_terminate,
    )
    await process.start()
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    owner.cancel()
    done, _pending = await asyncio.wait({owner}, timeout=0.5)
    assert owner in done
    assert owner.cancelled()

    assert process.stop_unconfirmed is True
    assert calls == [1]
    with pytest.raises(MCPConnectionError, match="awaiting stop finalization"):
        await process.start()
    assert process._owner_task is owner  # pyright: ignore[reportPrivateUsage]

    await process.stop()
    assert calls == [1]
    assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_owner_cancellation_fails_closed_before_context_exit() -> None:
    """Owner cancellation signals its PID hook before a slow context exit."""
    exit_started = asyncio.Event()
    release_exit = asyncio.Event()
    force_terminate_calls: list[int] = []

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    class SlowCancellationExit:
        async def __aenter__(self) -> FakeInitializableSession:
            return FakeInitializableSession(info_result())

        async def __aexit__(self, *exc_info: object) -> None:
            exit_started.set()
            await release_exit.wait()

    process = MCPServerProcess(
        session_factory=lambda _params: SlowCancellationExit(),
        force_terminate=force_terminate,
    )
    await process.start()
    owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert owner is not None

    try:
        owner.cancel()
        await asyncio.wait_for(exit_started.wait(), timeout=0.5)
        assert force_terminate_calls == [1]
        assert process.stop_unconfirmed is True
        assert not owner.done()
    finally:
        release_exit.set()
        done, _pending = await asyncio.wait({owner}, timeout=0.5)
        if owner in done:
            with suppress(BaseException):
                owner.result()
        await process.stop()

    assert owner.done()
    assert owner.cancelled()
    assert force_terminate_calls == [1]


@pytest.mark.asyncio
async def test_old_stop_finalizer_cannot_clobber_a_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed owner blocks start, and an old finalizer cannot clear its replacement."""
    real_wait = asyncio.wait
    old_wait_returned = asyncio.Event()
    allow_old_stop_to_finish = asyncio.Event()
    pause_next_wait = True

    async def pausing_wait(
        tasks: set[asyncio.Task[None]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal pause_next_wait
        result = await real_wait(tasks, timeout=timeout)
        if pause_next_wait:
            pause_next_wait = False
            old_wait_returned.set()
            await allow_old_stop_to_finish.wait()
        return result

    monkeypatch.setattr(asyncio, "wait", pausing_wait)
    process = MCPServerProcess(
        session_factory=FactoryProbe(FakeInitializableSession(info_result()))
    )
    old_stop: asyncio.Task[None] | None = None
    replacement_owner: asyncio.Task[None] | None = None
    try:
        await process.start()
        old_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
        assert old_owner is not None
        old_stop = asyncio.create_task(process.stop())
        await asyncio.wait_for(old_wait_returned.wait(), timeout=0.5)
        assert old_owner.done()

        with pytest.raises(MCPConnectionError, match="awaiting stop finalization"):
            await process.start()
        assert process._owner_task is old_owner  # pyright: ignore[reportPrivateUsage]

        # A second stop finalizes the completed owner while the first stop is
        # paused after observing the same generation.
        await process.stop()
        assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]

        await process.start()
        replacement_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
        assert replacement_owner is not None
        assert replacement_owner is not old_owner
        assert process.running

        allow_old_stop_to_finish.set()
        await asyncio.wait_for(old_stop, timeout=0.5)
        old_stop = None
        assert process._owner_task is replacement_owner  # pyright: ignore[reportPrivateUsage]
        assert process.running
    finally:
        monkeypatch.setattr(asyncio, "wait", real_wait)
        allow_old_stop_to_finish.set()
        if old_stop is not None:
            with suppress(BaseException):
                await asyncio.wait_for(old_stop, timeout=0.5)
        if process._owner_task is not None:  # pyright: ignore[reportPrivateUsage]
            await process.stop()

    assert replacement_owner is not None
    assert replacement_owner.done()


@pytest.mark.asyncio
async def test_old_stop_body_cannot_fail_close_a_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old retry cannot use a replacement generation's PID hook."""
    release_old_owner = asyncio.Event()
    force_terminate_calls: list[int] = []

    def force_terminate() -> bool:
        force_terminate_calls.append(1)
        return True

    class DelayedCancellationContext:
        async def __aenter__(self) -> FakeInitializableSession:
            return FakeInitializableSession(info_result())

        async def __aexit__(self, *exc_info: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_old_owner.wait()
                raise

    clean_probe = FactoryProbe(FakeInitializableSession(info_result()))
    spawn_count = 0

    def session_factory(
        params: object,
    ) -> AbstractAsyncContextManager[InitializableSession]:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            return DelayedCancellationContext()
        return clean_probe(params)

    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=session_factory,
        force_terminate=force_terminate,
    )
    await process.start()
    old_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
    assert old_owner is not None
    real_wait = asyncio.wait
    retry_wait_returned = asyncio.Event()
    allow_retry_stop_to_finish = asyncio.Event()
    pause_next_wait = True

    async def pausing_wait(
        tasks: set[asyncio.Task[None]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal pause_next_wait
        result = await real_wait(tasks, timeout=timeout)
        if pause_next_wait:
            pause_next_wait = False
            retry_wait_returned.set()
            await allow_retry_stop_to_finish.wait()
        return result

    first_stop: asyncio.Task[None] | None = None
    retry_stop: asyncio.Task[None] | None = None
    replacement_owner: asyncio.Task[None] | None = None
    try:
        first_stop = asyncio.create_task(process.stop())
        await asyncio.sleep(0.01)
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop
        assert process.stop_unconfirmed is True
        assert force_terminate_calls == [1]

        monkeypatch.setattr(asyncio, "wait", pausing_wait)
        retry_stop = asyncio.create_task(process.stop())
        release_old_owner.set()
        await asyncio.wait_for(retry_wait_returned.wait(), timeout=0.5)
        assert old_owner.cancelled()

        with pytest.raises(MCPConnectionError, match="awaiting stop finalization"):
            await process.start()
        assert process._owner_task is old_owner  # pyright: ignore[reportPrivateUsage]
        assert process.stop_unconfirmed is True

        # A second stop finalizes the retained owner while this retry is paused
        # after observing the same generation.
        await process.stop()
        assert process._owner_task is None  # pyright: ignore[reportPrivateUsage]
        assert force_terminate_calls == [1]

        incident_id = process.teardown_incident_id
        assert incident_id is not None
        with pytest.raises(MCPConnectionError, match="hardware-clear acknowledgement"):
            await process.start()
        process.acknowledge_hardware_clear(incident_id)
        await process.start()
        replacement_owner = process._owner_task  # pyright: ignore[reportPrivateUsage]
        assert replacement_owner is not None
        assert replacement_owner is not old_owner
        assert process.running
        assert process.stop_unconfirmed is False
        assert force_terminate_calls == [1]

        allow_retry_stop_to_finish.set()
        await asyncio.wait_for(retry_stop, timeout=0.5)
        retry_stop = None
        assert process._owner_task is replacement_owner  # pyright: ignore[reportPrivateUsage]
        assert process.running
        assert process.stop_unconfirmed is False
        assert force_terminate_calls == [1]
    finally:
        monkeypatch.setattr(asyncio, "wait", real_wait)
        release_old_owner.set()
        allow_retry_stop_to_finish.set()
        for task in (first_stop, retry_stop):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(BaseException):
                    await asyncio.wait_for(task, timeout=0.5)
        if process._owner_task is not None:  # pyright: ignore[reportPrivateUsage]
            await process.stop()

    assert replacement_owner is not None
    assert replacement_owner.done()


@pytest.mark.asyncio
async def test_stop_swallows_a_raising_force_terminate_hook() -> None:
    """Contract-hardening (#212/#365): a buggy force-terminate hook that
    raises must NOT propagate out of teardown — stop() still returns, sets
    stop_unconfirmed, and clears state, because it is on the fail-closed
    shutdown path that must always let the agent exit."""

    def force_terminate() -> bool:
        raise RuntimeError("hook blew up")

    session = FakeInitializableSession(info_result())
    probe = WedgedFactoryProbe(session)
    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=probe,
        force_terminate=force_terminate,
    )
    await process.start()
    await asyncio.wait_for(process.stop(), timeout=1.0)  # no raise escapes
    assert process.stop_unconfirmed is True
    assert not process.running


@pytest.mark.asyncio
async def test_start_requires_acknowledgement_after_unconfirmed_teardown() -> None:
    """A reused process cannot discard an unaudited teardown incident (#668)."""
    session = FakeInitializableSession(info_result())
    process = MCPServerProcess(
        MCPConfig(stop_timeout_seconds=0.05),
        session_factory=WedgedFactoryProbe(session),
        force_terminate=lambda: True,
    )
    await process.start()
    await asyncio.wait_for(process.stop(), timeout=1.0)
    assert process.stop_unconfirmed is True  # first teardown went unconfirmed

    incident_id = process.teardown_incident_id
    assert incident_id is not None
    with pytest.raises(MCPConnectionError, match="hardware-clear acknowledgement"):
        await process.start()
    assert process.stop_unconfirmed is True

    process.acknowledge_hardware_clear(incident_id)
    await process.start()
    try:
        assert process.stop_unconfirmed is False
    finally:
        await asyncio.wait_for(process.stop(), timeout=1.0)


@pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="POSIX process-group kill only (#212 targets darwin/linux)"
)
def test_force_terminate_process_group_kills_a_real_child() -> None:
    """The real force-kill: spawn a child in its own session (as the MCP
    transport does, ``start_new_session=True``) and assert the helper
    SIGKILLs the group, returning True (#212)."""
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert force_terminate_process_group(child.pid) is True
        # The child is reaped within a beat; -9 is SIGKILL on POSIX.
        assert child.wait(timeout=5.0) == -signal.SIGKILL
    finally:
        if child.poll() is None:  # pragma: no cover - defensive cleanup
            child.kill()
            child.wait(timeout=5.0)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process-group kill only")
def test_force_terminate_process_group_returns_false_when_already_gone() -> None:
    """An already-exited child yields False (ProcessLookupError path) — the
    hook reports it delivered no signal so callers don't over-claim (#212)."""
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", ""],
        start_new_session=True,
    )
    child.wait(timeout=5.0)
    # Give the OS a beat to release the now-dead group before we target it.
    for _ in range(50):
        if not _pgid_alive(child.pid):
            break
        time.sleep(0.02)
    assert force_terminate_process_group(child.pid) is False


def _pgid_alive(pid: int) -> bool:
    """Whether the process group led by ``pid`` still exists (test helper)."""
    try:
        os.killpg(os.getpgid(pid), 0)
    except (ProcessLookupError, OSError):
        return False
    return True


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
    "set_recording_metadata": SetRecordingMetadataResult,
}


def test_every_tool_has_a_captured_fixture() -> None:
    """One example per tool result shape (E5-S3 criterion) — exactly the
    14-tool surface, no strays."""
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


# --- E9: controller-protocol adapter + RoastSessionState projection ---


def _state_payload(elapsed: float, **overrides: object) -> dict[str, object]:
    payload = dict(SESSION_STATE_PAYLOAD)
    payload["elapsed_monotonic_seconds"] = elapsed
    payload.update(overrides)
    return payload


class _SequenceCaller:
    """Returns the next payload per call; the last one repeats."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool, arguments))
        index = min(len(self.calls) - 1, len(self._payloads) - 1)
        return self._payloads[index]


class _StepClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_project_session_state_maps_detection_and_passthrough() -> None:
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    telemetry = project_session_state(state, age_seconds=2.5)
    assert telemetry is not None
    assert (telemetry.bean_temp_c, telemetry.env_temp_c) == (196.0, 214.0)
    assert telemetry.age_seconds == 2.5
    assert telemetry.bean_ror_c_per_min == 8.2  # passthrough, never recomputed
    assert telemetry.t0_detected is True  # t0_status.status == "detected"
    assert telemetry.first_crack_detected is True


def test_project_session_state_none_without_usable_reading() -> None:
    no_device = RoastSessionState.model_validate({**SESSION_STATE_PAYLOAD, "device_state": None})
    assert project_session_state(no_device, age_seconds=0.0) is None
    null_temp_device = {
        **cast("dict[str, object]", SESSION_STATE_PAYLOAD["device_state"]),
        "bean_temp_c": None,
    }
    null_temp = RoastSessionState.model_validate(
        {**SESSION_STATE_PAYLOAD, "device_state": null_temp_device}
    )
    assert project_session_state(null_temp, age_seconds=0.0) is None


def test_project_session_state_pending_detection_is_false() -> None:
    payload = _state_payload(
        100.0,
        t0_status={
            **cast("dict[str, object]", SESSION_STATE_PAYLOAD["t0_status"]),
            "status": "pending",
        },
        first_crack_status={
            **cast("dict[str, object]", SESSION_STATE_PAYLOAD["first_crack_status"]),
            "status": "pending",
        },
    )
    state = RoastSessionState.model_validate(payload)
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.t0_detected is False
    assert telemetry.first_crack_detected is False


def _state_with_fc(**fc_overrides: object) -> RoastSessionState:
    """A session state with the first-crack status fields overridden (#197)."""
    payload = _state_payload(
        100.0,
        first_crack_status={
            **cast("dict[str, object]", SESSION_STATE_PAYLOAD["first_crack_status"]),
            **fc_overrides,
        },
    )
    return RoastSessionState.model_validate(payload)


# --- #337: backdating-delta projection (coffee-roaster-mcp v0.1.7) ---


def _beans_added_event(payload: dict[str, object]) -> dict[str, object]:
    """A backdated ``beans_added`` event snapshot dict with ``monotonic_seconds``
    at the turning point (the v0.1.7 server sets the event time to the onset)."""
    return {
        "kind": "beans_added",
        "recorded_at_utc": "2026-06-07T12:09:10.189739+00:00",
        "monotonic_seconds": 638.88,
        "payload": payload,
    }


def _first_crack_event(payload: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "first_crack_detected",
        "recorded_at_utc": "2026-06-07T12:18:11.708550+00:00",
        "monotonic_seconds": 1180.4,
        "payload": payload,
    }


def test_event_backdate_seconds_t0_from_payload_pair() -> None:
    """The T0 delta is ``confirmed_at − turning_point``, both MCP-domain — a
    duration, never an absolute timestamp."""
    event = EventSnapshot.model_validate(
        _beans_added_event(
            {
                "turning_point_monotonic_seconds": 638.88,
                "confirmed_at_monotonic_seconds": 655.88,
            }
        )
    )
    assert event_backdate_seconds(
        event, onset_key="turning_point_monotonic_seconds"
    ) == pytest.approx(17.0)


def test_event_backdate_seconds_fc_from_payload_pair() -> None:
    event = EventSnapshot.model_validate(
        _first_crack_event(
            {
                "detected_at_monotonic_seconds": 1180.4,
                "confirmed_at_monotonic_seconds": 1195.4,
            }
        )
    )
    assert event_backdate_seconds(
        event, onset_key="detected_at_monotonic_seconds"
    ) == pytest.approx(15.0)


def test_event_backdate_seconds_falls_back_to_event_monotonic_for_onset() -> None:
    """With no onset key in the payload the event's own (backdated)
    ``monotonic_seconds`` supplies the onset."""
    event = EventSnapshot.model_validate(
        _beans_added_event({"confirmed_at_monotonic_seconds": 650.88})
    )
    assert event_backdate_seconds(
        event, onset_key="turning_point_monotonic_seconds"
    ) == pytest.approx(12.0)


def test_event_backdate_seconds_none_without_confirmation_field() -> None:
    """A manual mark / pre-0.1.7 payload has no ``confirmed_at_monotonic_seconds``
    ⇒ no delta (the controller stamps at receive-tick)."""
    event = EventSnapshot.model_validate(_beans_added_event({}))
    assert event_backdate_seconds(event, onset_key="turning_point_monotonic_seconds") is None


def test_event_backdate_seconds_none_for_negative_delta() -> None:
    """A confirmation EARLIER than the onset is malformed ⇒ no delta (never
    fabricate a future-referenced anchor)."""
    event = EventSnapshot.model_validate(
        _beans_added_event(
            {
                "turning_point_monotonic_seconds": 700.0,
                "confirmed_at_monotonic_seconds": 650.0,
            }
        )
    )
    assert event_backdate_seconds(event, onset_key="turning_point_monotonic_seconds") is None


def test_event_backdate_seconds_none_for_non_numeric_confirmation() -> None:
    """A bool / non-numeric confirmation value is rejected (a delta must be a real
    duration)."""
    event = EventSnapshot.model_validate(
        _beans_added_event({"confirmed_at_monotonic_seconds": True})
    )
    assert event_backdate_seconds(event, onset_key="turning_point_monotonic_seconds") is None


def test_event_backdate_seconds_none_for_non_finite_confirmation() -> None:
    """A non-finite confirmation value (inf/NaN) collapses to no delta — the
    ``_payload_float`` finiteness guard, so a garbage timestamp never poisons the
    clock origin."""
    event = EventSnapshot.model_validate(
        _beans_added_event(
            {
                "turning_point_monotonic_seconds": 638.88,
                "confirmed_at_monotonic_seconds": float("inf"),
            }
        )
    )
    assert event_backdate_seconds(event, onset_key="turning_point_monotonic_seconds") is None


def test_event_backdate_seconds_none_for_non_finite_event_monotonic_fallback() -> None:
    """When the onset key is absent AND the event's own ``monotonic_seconds`` is
    non-finite, the fallback collapses to no delta rather than subtracting inf."""
    event = EventSnapshot.model_validate(
        {
            "kind": "beans_added",
            "recorded_at_utc": "2026-06-07T12:09:10.189739+00:00",
            "monotonic_seconds": float("inf"),
            "payload": {"confirmed_at_monotonic_seconds": 650.0},
        }
    )
    assert event_backdate_seconds(event, onset_key="turning_point_monotonic_seconds") is None


def test_project_session_state_surfaces_backdate_deltas() -> None:
    """The projection surfaces both deltas from the matching events so the
    controller can read them typed off ``RoastTelemetry``."""
    state = RoastSessionState.model_validate(
        _state_payload(
            1200.5,
            events=[
                _beans_added_event(
                    {
                        "turning_point_monotonic_seconds": 638.88,
                        "confirmed_at_monotonic_seconds": 655.88,
                    }
                ),
                _first_crack_event(
                    {
                        "detected_at_monotonic_seconds": 1180.4,
                        "confirmed_at_monotonic_seconds": 1195.4,
                    }
                ),
            ],
        )
    )
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.t0_backdate_seconds == pytest.approx(17.0)
    assert telemetry.first_crack_backdate_seconds == pytest.approx(15.0)


def test_project_session_state_backdate_none_for_legacy_empty_payload() -> None:
    """The default fixture's ``beans_added`` event carries an empty payload (the
    pre-0.1.7 / manual shape) ⇒ both deltas project as None."""
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.t0_backdate_seconds is None
    assert telemetry.first_crack_backdate_seconds is None


# --- #507: applied_state_from_event -----------------------------------------


def _drop_event(payload: dict[str, object]) -> EventSnapshot:
    return EventSnapshot.model_validate(
        {
            "kind": "beans_dropped",
            "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
            "monotonic_seconds": 1228.9,
            "payload": payload,
        }
    )


def test_applied_state_from_event_extracts_driver_reported_values() -> None:
    """The applied state comes from whatever the event payload actually
    carries — proving the extraction is a real read, not a hardcoded 0/100
    (#507's "never hardcode the driver's post-drop constants" direction)."""
    event = _drop_event({"heat_level_percent": 12, "fan_level_percent": 55, "cooling_on": True})
    assert applied_state_from_event(event) == AppliedRoasterState(
        heat_level_percent=12, fan_level_percent=55, cooling_on=True
    )


def test_applied_state_from_event_raises_for_missing_heat() -> None:
    event = _drop_event({"fan_level_percent": 100, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError, match="heat_level_percent"):
        applied_state_from_event(event)


def test_applied_state_from_event_raises_for_missing_fan() -> None:
    event = _drop_event({"heat_level_percent": 0, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError, match="fan_level_percent"):
        applied_state_from_event(event)


def test_applied_state_from_event_raises_for_missing_cooling_on() -> None:
    event = _drop_event({"heat_level_percent": 0, "fan_level_percent": 100})
    with pytest.raises(MalformedCommandResultError, match="cooling_on"):
        applied_state_from_event(event)


def test_applied_state_from_event_rejects_bool_heat() -> None:
    """``bool`` is an ``int`` subclass — must not silently pass as heat."""
    event = _drop_event({"heat_level_percent": True, "fan_level_percent": 100, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError, match="heat_level_percent"):
        applied_state_from_event(event)


def test_applied_state_from_event_rejects_non_bool_cooling_on() -> None:
    event = _drop_event({"heat_level_percent": 0, "fan_level_percent": 100, "cooling_on": "true"})
    with pytest.raises(MalformedCommandResultError, match="cooling_on"):
        applied_state_from_event(event)


# --- Codex follow-up on #509/#507: a well-typed-but-out-of-range value (int,
# but outside AppliedRoasterState's 0-100 bound) must translate into
# MalformedCommandResultError too, not escape as a raw pydantic
# ValidationError — the single choke point every caller (the adapter,
# the replay fallback) catches only MalformedCommandResultError.


def test_applied_state_from_event_raises_malformed_for_heat_above_100() -> None:
    event = _drop_event({"heat_level_percent": 101, "fan_level_percent": 100, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError, match="heat_level_percent"):
        applied_state_from_event(event)


def test_applied_state_from_event_raises_malformed_for_fan_below_0() -> None:
    event = _drop_event({"heat_level_percent": 0, "fan_level_percent": -1, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError, match="fan_level_percent"):
        applied_state_from_event(event)


def test_applied_state_from_event_raises_malformed_for_both_out_of_range() -> None:
    """Both heat and fan out of range together — a single
    MalformedCommandResultError still fires (pydantic reports both
    violations; the wrapping error need not enumerate each one, only never
    let the raw ValidationError itself escape)."""
    event = _drop_event({"heat_level_percent": 250, "fan_level_percent": -5, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError):
        applied_state_from_event(event)


def test_applied_state_from_event_out_of_range_error_chains_the_validation_error() -> None:
    """The translated error keeps the original ``ValidationError`` as its
    cause (``raise ... from exc``) — never silently swallows the pydantic
    detail, just re-types it into the one exception callers catch."""
    event = _drop_event({"heat_level_percent": 101, "fan_level_percent": 100, "cooling_on": True})
    with pytest.raises(MalformedCommandResultError) as excinfo:
        applied_state_from_event(event)
    assert isinstance(excinfo.value.__cause__, ValidationError)


# --- #507 safety-review MEDIUM: the adapter degrades a malformed payload to
# None (never raises) — the hardware command already succeeded by the time
# this parsing runs, so a payload parse failure must not surface as a
# caller-side exception (which every drop/e-stop caller treats as "the write
# itself failed").


@pytest.mark.asyncio
async def test_adapter_drop_beans_returns_none_on_malformed_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``RoasterControlAdapter.drop_beans()`` degrades a malformed
    ``beans_dropped`` payload to ``None`` (never raises) and logs a WARNING
    naming the malformed event's payload KEYS (never fabricating a value)."""
    caller = FakeToolCaller()
    caller_calls_payload = {
        "session_id": "abc",
        "phase": "dropped",
        "event": {
            "kind": "beans_dropped",
            "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
            "monotonic_seconds": 1228.9,
            # Malformed: cooling_on missing entirely.
            "payload": {"heat_level_percent": 0, "fan_level_percent": 100},
        },
        "event_count": 3,
    }

    async def caller_fn(tool: str, arguments: dict[str, object]) -> object:
        if tool == "drop_beans":
            return caller_calls_payload
        return await caller(tool, arguments)

    adapter = RoasterControlAdapter(RoasterMCPClient(caller_fn))
    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.mcp_client"):
        result = await adapter.drop_beans()
    assert result is None
    assert any(
        "beans_dropped" in record.message and "cooling_on" not in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), "WARNING must name the malformed event kind, not fabricate a cooling_on value"
    # The log carries only PAYLOAD KEYS, never values — the keys present here
    # are heat_level_percent/fan_level_percent (cooling_on absent).
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fan_level_percent" in m and "heat_level_percent" in m for m in warning_messages)


@pytest.mark.asyncio
async def test_adapter_emergency_stop_returns_none_on_malformed_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors the drop test above for ``emergency_stop`` — a malformed
    ``fault`` event payload degrades to ``None``, not an exception."""

    async def caller_fn(tool: str, arguments: dict[str, object]) -> object:
        if tool == "emergency_stop":
            return {
                "session_id": "abc",
                "phase": "fault",
                "event": {
                    "kind": "fault",
                    "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
                    "monotonic_seconds": 1228.9,
                    # Malformed: heat_level_percent is a bool, not an int.
                    "payload": {
                        "heat_level_percent": True,
                        "fan_level_percent": 100,
                        "cooling_on": True,
                    },
                },
                "event_count": 3,
            }
        raise AssertionError(f"unexpected tool call: {tool}")

    adapter = RoasterControlAdapter(RoasterMCPClient(caller_fn))
    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.mcp_client"):
        result = await adapter.emergency_stop(reason="test")
    assert result is None
    assert any(
        record.levelno == logging.WARNING and "fault" in record.getMessage()
        for record in caplog.records
    )


def test_applied_state_or_none_returns_parsed_state_on_a_well_formed_payload() -> None:
    """The happy path still returns the real parsed state (not always None) —
    proves ``_applied_state_or_none`` only degrades on genuine malformation."""
    event = _drop_event({"heat_level_percent": 12, "fan_level_percent": 55, "cooling_on": True})
    result = RoasterControlAdapter._applied_state_or_none(  # pyright: ignore[reportPrivateUsage]
        event
    )
    assert result == AppliedRoasterState(
        heat_level_percent=12, fan_level_percent=55, cooling_on=True
    )


def test_applied_state_or_none_degrades_out_of_range_heat_to_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex follow-up: an out-of-range (but well-typed) heat value must
    degrade to ``None`` + WARNING through the adapter, exactly like a
    missing/wrong-type field — never let the pydantic ``ValidationError``
    escape ``_applied_state_or_none``."""
    event = _drop_event({"heat_level_percent": 101, "fan_level_percent": 100, "cooling_on": True})
    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.mcp_client"):
        result = RoasterControlAdapter._applied_state_or_none(  # pyright: ignore[reportPrivateUsage]
            event
        )
    assert result is None
    assert any(
        record.levelno == logging.WARNING and "beans_dropped" in record.getMessage()
        for record in caplog.records
    )


def test_project_mic_status_running_and_detecting_is_ok() -> None:
    """audio_running + pending/detected → OK (green), counters forwarded (#197)."""
    state = _state_with_fc(status="detected", audio_running=True, emitted_window_count=311)
    mic = project_mic_status(state.first_crack_status)
    assert mic.mic_health is MicHealth.OK
    assert mic.audio_running is True
    assert mic.fc_status == "detected"
    assert mic.emitted_window_count == 311  # forwarded, not recomputed

    pending = _state_with_fc(status="pending", audio_running=True)
    assert project_mic_status(pending.first_crack_status).mic_health is MicHealth.OK


def test_project_mic_status_faulted_or_unavailable_is_error() -> None:
    """faulted / unavailable → ERROR (red), regardless of audio_running (#197)."""
    faulted = _state_with_fc(status="faulted", audio_running=False, reason="device busy")
    error = project_mic_status(faulted.first_crack_status)
    assert error.mic_health is MicHealth.ERROR
    assert error.reason == "device busy"

    # ERROR wins even if the capture loop reports running but the status faulted.
    unavailable = _state_with_fc(status="unavailable", audio_running=True)
    assert project_mic_status(unavailable.first_crack_status).mic_health is MicHealth.ERROR


def test_project_mic_status_disabled_or_manual_is_idle() -> None:
    """disabled / manual mode, or capture not yet running → IDLE (amber) (#197)."""
    disabled = _state_with_fc(mode="disabled", status="disabled", audio_running=False)
    assert project_mic_status(disabled.first_crack_status).mic_health is MicHealth.IDLE

    manual = _state_with_fc(mode="manual", status="manual", audio_running=False)
    assert project_mic_status(manual.first_crack_status).mic_health is MicHealth.IDLE

    # pending but capture not yet alive is IDLE, not OK (audio_running gates OK).
    not_running = _state_with_fc(status="pending", audio_running=False)
    assert project_mic_status(not_running.first_crack_status).mic_health is MicHealth.IDLE


def test_project_session_state_carries_mic_status() -> None:
    """The telemetry projection rides mic_status alongside detection (#197)."""
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.mic_status is not None
    assert telemetry.mic_status.mic_health is MicHealth.OK


def test_project_mic_status_forwards_the_overflow_diagnostics_trio() -> None:
    """#539: project_mic_status forwards the MCP 0.1.13 overflow diagnostics
    (coffee-roaster-mcp#190) 1:1 — this was the exact gap #538 left (the
    fields were parsed into the FirstCrackStatus mirror but dropped here)."""
    state = _state_with_fc(
        status="detected",
        audio_running=True,
        overflow_count_last_minute=4,
        estimated_lost_audio_ms_last_minute=96.5,
        total_overflow_count=17,
    )
    mic = project_mic_status(state.first_crack_status)
    assert mic.overflow_count_last_minute == 4
    assert mic.estimated_lost_audio_ms_last_minute == 96.5
    assert mic.total_overflow_count == 17


def test_project_mic_status_overflow_diagnostics_default_to_zero_pre_0_1_13() -> None:
    """#539: a pre-0.1.13 MCP payload (all-zero overflow fields, the mirror's
    own defaults — see test_first_crack_status_overflow_fields_default_for_
    pre_0113_payloads) projects cleanly through to MicStatus's matching
    defaults, never a validation failure."""
    state = _state_with_fc(
        status="detected",
        audio_running=True,
        overflow_count_last_minute=0,
        estimated_lost_audio_ms_last_minute=0.0,
        total_overflow_count=0,
    )
    mic = project_mic_status(state.first_crack_status)
    assert mic.overflow_count_last_minute == 0
    assert mic.estimated_lost_audio_ms_last_minute == 0.0
    assert mic.total_overflow_count == 0


def test_project_live_ambient_ok_status_passes_through_triad() -> None:
    """#464 (D86): an ``"ok"`` ambient status yields the live triad verbatim."""
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    assert project_live_ambient(state.ambient_status) == (28.49, 38.6, 1008.56)


def test_project_recordable_ambient_is_strictly_narrower_than_live() -> None:
    """#745: the recordable predicate adds a dateability clause to the live one.

    "Strictly narrower" is the load-bearing property — it is what closes the hole
    between #745's two fixes, where a reading the live advisor declines could
    still be persisted as the run's corpus breadcrumb. Asserted directly here
    rather than only through the two api-level capture tests, so the relationship
    between the two predicates is pinned rather than inferred (safety-reviewer
    finding 1, folded pre-open).

    The four cases are the full cross-product that matters: the healthy one, and
    each way a reading can be unusable.
    """

    def _status(**overrides: object) -> AmbientStatus:
        payload = {
            **SESSION_STATE_PAYLOAD,
            "ambient_status": {
                **dict(SESSION_STATE_PAYLOAD["ambient_status"]),  # type: ignore[dict-item]
                **overrides,
            },
        }
        return RoastSessionState.model_validate(payload).ambient_status

    triad = (28.49, 38.6, 1008.56)
    nulls = (None, None, None)

    # ok + running + a finite stamp: both predicates agree on the triad.
    healthy = _status()
    assert project_live_ambient(healthy) == triad
    assert project_recordable_ambient(healthy) == triad

    # ok + running but UNDATEABLE: this is the narrowing. The live projection
    # still forwards it (the controller declines it on the age instead); the
    # recordable one must not persist it.
    undateable = _status(last_reading_monotonic_seconds=float("nan"))
    assert project_live_ambient(undateable) == triad
    assert project_recordable_ambient(undateable) == nulls

    # Stopped-but-"ok" runtime: both reject, inherited from project_live_ambient.
    stopped = _status(ambient_running=False)
    assert project_live_ambient(stopped) == nulls
    assert project_recordable_ambient(stopped) == nulls

    # No reading at all: the MCP nulls the stamp and the triad together.
    absent = _status(last_reading_monotonic_seconds=None)
    assert project_recordable_ambient(absent) == nulls


def test_project_live_ambient_non_ok_status_is_none() -> None:
    """#464 (D86): disabled/unavailable ambient degrades to an all-None triad,
    mirroring the MCP's own fail-soft contract (#342, D85) — never a fault."""
    disabled_payload = _state_payload(
        100.0,
        ambient_status={
            "mode": "disabled",
            "status": "disabled",
            "reason": "Ambient sensing is disabled by configuration.",
            "ambient_running": False,
        },
    )
    disabled = RoastSessionState.model_validate(disabled_payload)
    assert project_live_ambient(disabled.ambient_status) == (None, None, None)

    unavailable_payload = _state_payload(
        100.0,
        ambient_status={
            "mode": "yoctopuce",
            "status": "unavailable",
            "reason": "Yoctopuce probe not detected.",
            "ambient_running": False,
        },
    )
    unavailable = RoastSessionState.model_validate(unavailable_payload)
    assert project_live_ambient(unavailable.ambient_status) == (None, None, None)


def test_project_session_state_carries_live_ambient_when_ok() -> None:
    """#464 (D86): the telemetry projection rides the live ambient triad
    alongside mic_status, mirroring the same precedent (#197)."""
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.ambient_temp_c == 28.49
    assert telemetry.ambient_humidity_pct == 38.6
    assert telemetry.ambient_pressure_hpa == 1008.56


def test_project_session_state_ambient_none_when_disabled() -> None:
    """#464 (D86): a disabled/unavailable ambient config projects to None on
    RoastTelemetry — fail-soft, never a crash or a fault."""
    payload = _state_payload(
        100.0,
        ambient_status={
            "mode": "disabled",
            "status": "disabled",
            "reason": "Ambient sensing is disabled by configuration.",
            "ambient_running": False,
        },
    )
    state = RoastSessionState.model_validate(payload)
    telemetry = project_session_state(state, age_seconds=0.0)
    assert telemetry is not None
    assert telemetry.ambient_temp_c is None
    assert telemetry.ambient_humidity_pct is None
    assert telemetry.ambient_pressure_hpa is None


def test_project_live_ambient_none_when_the_runtime_has_stopped() -> None:
    """#732: ``status == "ok"`` alone does NOT mean the reading is live.

    The MCP's ``AmbientSessionRuntime._stop_locked`` drops its reader
    (``ambient_running`` -> ``False``) while deliberately leaving ``status`` at
    ``"ok"`` and preserving the last reading — and ``poll``'s
    ``self._reader is None`` early return then means that reading can never
    change again. The old status-only gate forwarded that frozen room
    temperature for the rest of the roast, indistinguishable from a fresh one.

    Bounded while ambient was observability-only; not bounded once c11 (#709)
    selects a fan regime on it."""
    payload = _state_payload(
        100.0,
        ambient_status={
            "mode": "yoctopuce",
            "status": "ok",
            "reason": "Ambient sensing stopped: session ended.",
            "ambient_running": False,
            "temperature_c": 23.1,
            "humidity_percent": 35.0,
            "pressure_hpa": 1011.0,
            "last_reading_monotonic_seconds": 1200.0,
        },
    )
    state = RoastSessionState.model_validate(payload)

    assert project_live_ambient(state.ambient_status) == (None, None, None)

    telemetry = project_session_state(state, age_seconds=0.0, ambient_age_seconds=1.0)
    assert telemetry is not None
    assert telemetry.ambient_temp_c is None
    # The age goes with the reading: no reading, no age to report.
    assert telemetry.ambient_age_seconds is None


@pytest.mark.asyncio
async def test_adapter_ambient_age_grows_while_the_reading_does_not_change() -> None:
    """#732: the adapter derives ambient freshness the same way it already
    derives ``age_seconds`` — and for the same reason.

    The MCP's ``last_reading_monotonic_seconds`` is an absolute stamp from
    ANOTHER PROCESS, so it is compared for CHANGE only and never subtracted
    from anything; the elapsed time is measured in the agent's own clock. Three
    reads: the first observes a reading, the second sees the same stamp (the
    probe has not refreshed — age grows), the third sees a new stamp (age
    resets)."""
    clock = _StepClock()
    unchanged = _state_payload(101.0)
    refreshed = _state_payload(
        102.0,
        ambient_status={
            **dict(SESSION_STATE_PAYLOAD["ambient_status"]),  # type: ignore[dict-item]
            "temperature_c": 24.0,
            "last_reading_monotonic_seconds": 1230.0,
        },
    )
    adapter = RoasterControlAdapter(
        RoasterMCPClient(_SequenceCaller([_state_payload(100.0), unchanged, refreshed])),
        clock=clock,
    )

    first = await adapter.read_telemetry()
    clock.now = 1.0
    second = await adapter.read_telemetry()
    clock.now = 2.0
    third = await adapter.read_telemetry()

    assert first is not None and first.ambient_age_seconds == 0.0
    # Same reading on the second read: it has now been current for one step.
    assert second is not None and second.ambient_age_seconds == 1.0
    # A genuinely new reading resets the age, and carries the new value.
    assert third is not None and third.ambient_age_seconds == 0.0
    assert third.ambient_temp_c == 24.0


@pytest.mark.asyncio
async def test_adapter_ambient_age_is_unknown_for_a_nan_reading_stamp() -> None:
    """#745b: a NaN stamp must not read as a permanently-fresh reading.

    The stamp is an opaque IDENTITY token compared with ``!=``, and NaN is
    unequal to itself under IEEE-754 — so an unguarded token makes every tick
    look like a brand-new reading: the age is re-based to ``now`` each tick and
    never leaves 0.0. The controller's range check is written to fail closed on
    a NaN *age*, but it never sees one; a frozen value stays "fresh" forever,
    which is the single thing the freshness clock exists to prevent.

    Rejecting the stamp makes it read as "no reading", so the age is ``None``
    (age-unknown) and the controller declines — ``c11`` takes its absent-ambient
    path, the #498-safe direction.

    Two reads with the SAME NaN stamp: an unguarded token would report 0.0 twice
    (each read looking new); a token-with-guard reports unknown twice.
    """
    clock = _StepClock()
    nan_stamp = {
        **dict(SESSION_STATE_PAYLOAD["ambient_status"]),  # type: ignore[dict-item]
        "last_reading_monotonic_seconds": float("nan"),
    }
    adapter = RoasterControlAdapter(
        RoasterMCPClient(
            _SequenceCaller(
                [
                    _state_payload(100.0, ambient_status=nan_stamp),
                    _state_payload(101.0, ambient_status=nan_stamp),
                ]
            )
        ),
        clock=clock,
    )

    first = await adapter.read_telemetry()
    clock.now = 1.0
    second = await adapter.read_telemetry()

    assert first is not None and first.ambient_age_seconds is None
    assert second is not None and second.ambient_age_seconds is None


def test_ambient_reading_token_rejects_non_finite_stamps() -> None:
    """#745b at the boundary every consumer goes through.

    NaN is the one that defeats the equality mechanism; ``±inf`` compares equal
    to itself and so would not, but a non-finite stamp is malformed either way
    and there is no reason to carry one. A finite stamp — including ``0.0``,
    which must not be confused with absent — passes through unchanged.
    """

    def _status(stamp: float | None) -> AmbientStatus:
        payload = {
            **SESSION_STATE_PAYLOAD,
            "ambient_status": {
                **dict(SESSION_STATE_PAYLOAD["ambient_status"]),  # type: ignore[dict-item]
                "last_reading_monotonic_seconds": stamp,
            },
        }
        return RoastSessionState.model_validate(payload).ambient_status

    assert ambient_reading_token(_status(float("nan"))) is None
    assert ambient_reading_token(_status(float("inf"))) is None
    assert ambient_reading_token(_status(float("-inf"))) is None
    assert ambient_reading_token(_status(None)) is None
    # 0.0 is a legitimate stamp and must not be confused with absent.
    assert ambient_reading_token(_status(0.0)) == 0.0
    assert ambient_reading_token(_status(1230.0)) == 1230.0


@pytest.mark.asyncio
async def test_adapter_ambient_age_resets_on_a_new_session() -> None:
    """#732, post-open Codex P2: the ambient tracker is per-SESSION state.

    ``start_session`` resets the telemetry-age trackers; the ambient ones were
    added beside them and initially were not. That leaks across back-to-back
    roasts through one adapter, and the MCP makes the leak reachable rather
    than theoretical: its stop path deliberately PRESERVES the last reading and
    its stamp, and a stop/start pair need not have an intervening telemetry
    read. So the new session's first state can carry the previous roast's
    token — which either ages the new run's first reading from the old run's
    clock (passing a stale reading as fresh) or declines it at once and burns
    the new run's one-shot decline warning on a phantom.

    The stamp is deliberately IDENTICAL either side of the restart; a test that
    changed it would pass without the reset and prove nothing."""
    clock = _StepClock()

    async def call_tool(tool: str, arguments: dict[str, object]) -> object:
        # The restart itself goes through the real ``start_roast_session`` path,
        # so the reset under test is exercised where it actually lives.
        return CANNED[tool] if tool == "start_roast_session" else _state_payload(100.0)

    adapter = RoasterControlAdapter(RoasterMCPClient(call_tool), clock=clock)

    first = await adapter.read_telemetry()
    assert first is not None and first.ambient_age_seconds == 0.0

    # Time passes within the first roast, so a leaked tracker would report it.
    clock.now = 300.0
    await adapter.start_session()
    after_restart = await adapter.read_telemetry()

    assert after_restart is not None
    assert after_restart.ambient_temp_c == 28.49
    assert after_restart.ambient_age_seconds == 0.0


@pytest.mark.asyncio
async def test_adapter_ambient_age_resets_across_an_outage_on_the_same_reading() -> None:
    """#732: freshness is tracked for the reading the projection FORWARDS, not
    for whatever stamp the status carries.

    The sharp case is the stopped runtime, because ``_stop_locked`` PRESERVES
    the last reading: the MCP reports the same ``last_reading_monotonic_seconds``
    right through the outage. A tracker keyed on the stamp alone would keep
    ageing a reading nothing consumes, and on resume would hand back an age
    measured from before the outage — so a probe reporting a perfectly good
    reading would be declined by the controller's bound for as long as the gap
    lasted. Keyed on the forwarded reading, the outage resets it.

    The stamp is deliberately IDENTICAL in all three reads; a test that changed
    it would pass without the reset and prove nothing."""
    clock = _StepClock()
    running = dict(SESSION_STATE_PAYLOAD["ambient_status"])  # type: ignore[call-overload]
    stopped = _state_payload(
        101.0,
        ambient_status={
            **running,
            "ambient_running": False,
            "reason": "Ambient sensing stopped: session ended.",
        },
    )
    adapter = RoasterControlAdapter(
        RoasterMCPClient(_SequenceCaller([_state_payload(100.0), stopped, _state_payload(102.0)])),
        clock=clock,
    )

    await adapter.read_telemetry()
    clock.now = 45.0
    outage = await adapter.read_telemetry()
    clock.now = 90.0
    back = await adapter.read_telemetry()

    assert outage is not None and outage.ambient_temp_c is None
    assert outage.ambient_age_seconds is None
    assert back is not None and back.ambient_temp_c == 28.49
    assert back.ambient_age_seconds == 0.0


@pytest.mark.asyncio
async def test_adapter_age_resets_on_advance_grows_on_stall() -> None:
    """The stale-telemetry safety fault depends on a real age: it stays ~0 while
    the session clock advances and grows once it stalls."""
    clock = _StepClock()
    caller = _SequenceCaller([_state_payload(100.0), _state_payload(101.0), _state_payload(101.0)])
    adapter = RoasterControlAdapter(RoasterMCPClient(caller), clock=clock)

    first = await adapter.read_telemetry()
    assert first is not None and first.age_seconds == 0.0

    clock.now = 5.0
    second = await adapter.read_telemetry()  # elapsed advanced 100→101 → fresh
    assert second is not None and second.age_seconds == 0.0

    clock.now = 8.0
    third = await adapter.read_telemetry()  # elapsed stalled at 101 → ages
    assert third is not None and third.age_seconds == 3.0
    assert adapter.last_state is not None
    assert adapter.last_state.development_percent == 3.6


@pytest.mark.asyncio
async def test_adapter_set_targets_writes_heat_then_fan() -> None:
    caller = _SequenceCaller([CANNED["set_heat"], CANNED["set_fan"]])  # type: ignore[list-item]
    adapter = RoasterControlAdapter(RoasterMCPClient(caller))
    await adapter.set_targets(heat_percent=55, fan_percent=45)
    assert caller.calls == [
        ("set_heat", {"heat_level_percent": 55}),
        ("set_fan", {"fan_level_percent": 45}),
    ]


@pytest.mark.asyncio
async def test_adapter_read_telemetry_propagates_transport_error() -> None:
    """Transport failures must propagate so the controller's consecutive-failure
    rules see a read fault — never a silent reconnect-and-continue."""

    async def boom(tool: str, arguments: dict[str, object]) -> object:
        raise MCPConnectionError("dead child")

    adapter = RoasterControlAdapter(RoasterMCPClient(boom))
    with pytest.raises(MCPConnectionError):
        await adapter.read_telemetry()


@pytest.mark.asyncio
async def test_adapter_passes_through_all_commands() -> None:
    caller = FakeToolCaller()
    adapter = RoasterControlAdapter(RoasterMCPClient(caller))
    await adapter.start_session()
    await adapter.mark_beans_added()
    await adapter.mark_first_crack()
    drop_applied = await adapter.drop_beans()
    await adapter.start_cooling()
    await adapter.stop_cooling()
    estop_applied = await adapter.emergency_stop(reason="manual")
    result = await adapter.export_roast_log()
    assert [name for name, _ in caller.calls] == [
        "start_roast_session",
        "mark_beans_added",
        "mark_first_crack",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "export_roast_log",
    ]
    assert result.ready is True
    # #507: drop_beans/emergency_stop return the driver's applied state,
    # parsed from the command result's own event payload — not a constant.
    assert drop_applied == AppliedRoasterState(
        heat_level_percent=0, fan_level_percent=100, cooling_on=True
    )
    assert estop_applied == AppliedRoasterState(
        heat_level_percent=0, fan_level_percent=100, cooling_on=True
    )


@pytest.mark.asyncio
async def test_adapter_start_session_clears_cached_state() -> None:
    """A new session must not inherit the previous roast's cached read. A reused
    adapter (back-to-back roasts) would otherwise expose the prior session's
    last_state — e.g. a stale mic icon, or stale raw_state_json/dev%/phase
    persisted for the new run's first tick — until the next read (#200/Codex)."""
    adapter = RoasterControlAdapter(RoasterMCPClient(FakeToolCaller()))
    await adapter.read_telemetry()  # a prior roast leaves cached state
    assert adapter.last_state is not None
    await adapter.start_session()  # the next session must start from a clean cache
    assert adapter.last_state is None


@pytest.mark.asyncio
async def test_adapter_sets_recording_metadata_before_session() -> None:
    """v0.1.9 (#176): set_recording_metadata must fire BEFORE start_roast_session
    — the MCP only applies the filename if metadata precedes the session."""
    caller = FakeToolCaller()
    adapter = RoasterControlAdapter(RoasterMCPClient(caller))
    await adapter.start_session(recording_origin="colombia-huila", recording_roast_num=5)
    assert caller.calls == [
        ("set_recording_metadata", {"origin": "colombia-huila", "roast_num": 5}),
        ("start_roast_session", {}),
    ]


@pytest.mark.asyncio
async def test_adapter_skips_recording_metadata_when_origin_missing() -> None:
    """No origin (or no roast_num) → skip the metadata call; the MCP falls back
    to its default recording naming."""
    caller = FakeToolCaller()
    adapter = RoasterControlAdapter(RoasterMCPClient(caller))
    await adapter.start_session()
    assert [name for name, _ in caller.calls] == ["start_roast_session"]


@pytest.mark.asyncio
async def test_adapter_recording_metadata_failure_never_blocks_roast() -> None:
    """Recording naming is best-effort: a set_recording_metadata failure is
    swallowed and the session still starts."""
    calls: list[str] = []

    async def caller(tool: str, arguments: dict[str, object]) -> object:
        calls.append(tool)
        if tool == "set_recording_metadata":
            raise MCPToolError("metadata rejected")
        return CANNED[tool]

    adapter = RoasterControlAdapter(RoasterMCPClient(caller))
    await adapter.start_session(recording_origin="colombia-huila", recording_roast_num=5)
    # The metadata attempt failed but the session still opened.
    assert calls == ["set_recording_metadata", "start_roast_session"]


def test_first_crack_status_overflow_fields_default_for_pre_0113_payloads() -> None:
    """A pre-0.1.13 fc_status payload (no overflow keys) still validates.

    MCP 0.1.13 added the #190 overflow diagnostics; older captures and any
    not-yet-upgraded server omit them, so the mirror must default rather
    than reject (the same additive-fields contract the ambient triad used).
    """
    from roastpilot_agent.mcp_client import FirstCrackStatus

    legacy = {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "allow_manual_override": True,
    }
    status = FirstCrackStatus.model_validate(legacy)
    assert status.overflow_count_last_minute == 0
    assert status.estimated_lost_audio_ms_last_minute == 0.0
    assert status.total_overflow_count == 0


def test_first_crack_status_overflow_fields_round_trip() -> None:
    """0.1.13 overflow values survive mirror validation verbatim."""
    from roastpilot_agent.mcp_client import FirstCrackStatus

    payload = {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "allow_manual_override": True,
        "overflow_count_last_minute": 4,
        "estimated_lost_audio_ms_last_minute": 812.5,
        "total_overflow_count": 19,
    }
    status = FirstCrackStatus.model_validate(payload)
    assert status.overflow_count_last_minute == 4
    assert status.estimated_lost_audio_ms_last_minute == 812.5
    assert status.total_overflow_count == 19
