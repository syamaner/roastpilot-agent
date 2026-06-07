"""E5-S1: MCP mirrors and typed tool wrappers (component plan §2, §8).

Child-process lifecycle (E5-S2) and per-tool captured fixtures (E5-S3)
extend this suite. Mirror shapes are derived from the coffee-roaster-mcp
source and validated here against the 7 Jun 2026 live-roast exports.
"""

import json
from pathlib import Path

import pytest

from roastpilot_agent.mcp_client import (
    ControlCommandResult,
    EventCommandResult,
    EventSnapshot,
    ExportRoastLogResult,
    RoasterMCPClient,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
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
        "current_phase": "Bootstrap",
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
