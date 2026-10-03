"""Behavioural safety tests for the cold-characterisation MCP boundary."""

import ast
import asyncio
import inspect
import json
import traceback
import types
import typing
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation import mcp as cold_mcp
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_JSON_KEY_BYTES,
    MAX_RAW_AUDIO_EXTRA_BYTES,
    MAX_VENDOR_BLOB_BYTES,
    ColdAudioField,
    ColdEvidenceFailure,
    ColdJsonValue,
    ColdPhaseKind,
    ColdTickAudioSample,
    ColdTickDeviceEvidence,
    ColdTickProjection,
    ColdTickRecord,
    ColdTickRoastFanEvidence,
    ColdTickRoastFanOutcome,
    ColdTickSessionEvidence,
    ColdTickSessionPhase,
    _canonical_json,  # pyright: ignore[reportPrivateUsage]
    project_tick_audio,
    validate_record,
)
from roastpilot_agent.cold_characterisation.mcp import (
    COLD_ALLOWED_TOOLS,
    ColdCharacterisationMCPClient,
    ColdDeviceField,
    ColdDeviceProjectionFailure,
    ColdFinalisationNotCleanError,
    ColdFinalisationSafetyError,
    ColdMcpError,
    ColdMcpTransportError,
    ColdMcpValidationError,
    ColdModeForbiddenToolError,
    ColdRoastFanOutcome,
    ColdRoastFanProjectionFailure,
    ColdSessionField,
    ColdSessionIdentityError,
    ColdSessionPhaseError,
    ColdSessionPurposeError,
    ColdTickAudioProjectionError,
    ColdTickDeviceProjectionError,
    ColdTickDeviceState,
    ColdTickObservation,
    ColdTickRoastFanObservation,
    ColdTickRoastFanProjectionError,
    ColdTickSessionMetadata,
    ColdTickSessionProjectionError,
    ColdTickTemperatureProjectionError,
    RejectionReason,
    SessionFinalisationResult,
    _finalisation_has_capability_compatible_evidence,  # pyright: ignore[reportPrivateUsage]
    finalisation_command_streaming_observation,
    finalisation_is_clean,
)
from roastpilot_agent.cold_characterisation.temperature_projection import (
    FIELD_NAMES,
    ColdTemperatureProjectionFailure,
    ColdTickTemperatureProjection,
)
from roastpilot_agent.mcp_client import (
    MCPConnectionError,
    MCPServerProcess,
    MCPToolError,
    MCPToolTimeoutError,
    RoasterDeviceState,
    RoastSessionState,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
from tests.test_cold_characterisation_temperature_projection import (
    ACCEPTED_SHAPES,
    LEAF_FAILURES,
    celsius_agree,
)

_MCP_TOOL_FIXTURES = Path(__file__).parent / "fixtures" / "mcp-tool-results"
_FINALISATION_FIXTURE = _MCP_TOOL_FIXTURES / "finalise_cold_characterisation_session.json"
_COUNTERS = (
    "command_send_attempts",
    "command_write_count",
    "last_command_write_size",
    "command_loop_error_count",
    "status_packet_count",
    "status_read_error_count",
)
_DIMENSIONS = (
    "heat_level_percent",
    "roast_fan_level_percent",
    "main_fan_level_percent",
    "drum_motor_on",
    "cooling_motor_on",
    "solenoid_open",
)


def _payload() -> dict[str, object]:
    """Return a mutable copy of the captured published-MCP result."""
    payload = cast("dict[str, object]", json.loads(_FINALISATION_FIXTURE.read_text()))
    payload["session_id"] = "session-id"
    return payload


def _driver(payload: dict[str, object]) -> dict[str, object]:
    """Return the final strict driver evidence mapping from a valid payload."""
    final_read = cast("dict[str, object]", payload["final_driver_evidence"])
    return cast("dict[str, object]", final_read["evidence"])


def _streaming_payload() -> dict[str, object]:
    """Return a valid streaming-capability result for hardware-free testing."""
    payload = _payload()
    driver = _driver(payload)
    driver["command_streaming_required"] = True
    for counter in _COUNTERS:
        driver[counter] = 0
    disconnect = cast("dict[str, object]", payload["disconnect"])
    disconnect["command_loop_stopped"] = "confirmed"
    disconnect["serial_closed"] = "confirmed"
    return payload


class _Caller:
    """Recorded deterministic MCP transport for client-boundary tests."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, args: dict[str, object]) -> object:
        self.calls.append((tool, args))
        return self.result


class _MappingCaller:
    """Deterministic transport whose responses are selected by tool name."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, args: dict[str, object]) -> object:
        self.calls.append((tool, args))
        return self.responses[tool]


class _RaisingCaller:
    """Recorded transport that exposes only a caller-provided failure."""

    def __init__(self, error: MCPConnectionError) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, args: dict[str, object]) -> object:
        self.calls.append((tool, args))
        raise self.error


def _client_for(result: object) -> tuple[ColdCharacterisationMCPClient, _Caller]:
    """Build a cold client with a deterministic result supplier."""
    caller = _Caller(result)
    client = ColdCharacterisationMCPClient(caller)
    client._cold_session_id = "session-id"  # pyright: ignore[reportPrivateUsage]
    return client, caller


def _unstarted_client_for(result: object) -> tuple[ColdCharacterisationMCPClient, _Caller]:
    """Build a cold client with no established session identity."""
    caller = _Caller(result)
    return ColdCharacterisationMCPClient(caller), caller


def _cold_start_payload() -> dict[str, object]:
    """Return a start response that establishes the deterministic cold session."""
    payload = cast(
        "dict[str, object]",
        json.loads((_MCP_TOOL_FIXTURES / "start_roast_session.json").read_text()),
    )
    session = cast("dict[str, object]", payload["session"])
    session["session_id"] = "session-id"
    session["session_purpose"] = "cold_characterisation"
    return payload


def _cold_state_payload() -> dict[str, object]:
    """Return a state response for the deterministic established cold session."""
    payload = cast(
        "dict[str, object]", json.loads((_MCP_TOOL_FIXTURES / "get_roast_state.json").read_text())
    )
    payload["session_id"] = "session-id"
    payload["session_purpose"] = "cold_characterisation"
    payload["cold_characterisation_observation"] = {
        "outcome": "observed",
        "roast_fan_level_percent": 0,
    }
    payload["cold_temperature_projection"] = celsius_agree()
    return payload


def _cold_marked_payload() -> dict[str, object]:
    """Return a beans-added event response for the deterministic cold session."""
    payload = cast(
        "dict[str, object]",
        json.loads((_MCP_TOOL_FIXTURES / "mark_beans_added.json").read_text()),
    )
    payload["session_id"] = "session-id"
    return payload


def test_cold_tool_surface_is_exact_and_frozen() -> None:
    """G1/G2: the cold client exposes only its six non-actuating methods."""
    public_methods = {
        name
        for name, member in inspect.getmembers(ColdCharacterisationMCPClient)
        if not name.startswith("_") and callable(member)
    }
    assert public_methods == {
        "get_server_info",
        "get_runtime_config",
        "start_cold_session",
        "get_roast_state",
        "mark_beans_added",
        "finalise_session",
    }
    assert (
        frozenset(
            {
                "get_server_info",
                "get_runtime_config",
                "start_roast_session",
                "get_roast_state",
                "mark_beans_added",
                "finalise_cold_characterisation_session",
            }
        )
        == COLD_ALLOWED_TOOLS
    )


@pytest.mark.asyncio
async def test_cold_client_rejects_any_non_allowlisted_tool() -> None:
    """G1: exact membership rejects a routed-control near miss."""
    client, caller = _client_for({})
    with pytest.raises(ColdModeForbiddenToolError):
        await client._call("set_heat_v2", {})  # pyright: ignore[reportPrivateUsage]
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [MCPConnectionError, MCPToolTimeoutError, MCPToolError])
@pytest.mark.parametrize(
    ("method", "tool"),
    [
        ("get_server_info", "get_server_info"),
        ("get_runtime_config", "get_runtime_config"),
        ("start_cold_session", "start_roast_session"),
        ("get_roast_state", "get_roast_state"),
        ("mark_beans_added", "mark_beans_added"),
        ("finalise_session", "finalise_cold_characterisation_session"),
    ],
)
async def test_every_cold_method_contains_transport_failures(
    method: str, tool: str, error_type: type[MCPConnectionError]
) -> None:
    """Transport, server, and timeout failures never expose caller details."""
    marker = "transport-payload-marker"
    caller = _RaisingCaller(error_type(marker))
    client = ColdCharacterisationMCPClient(caller)
    if method != "start_cold_session":
        client._cold_session_id = "session-id"  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ColdMcpTransportError) as raised:
        if method == "get_server_info":
            await client.get_server_info()
        elif method == "get_runtime_config":
            await client.get_runtime_config()
        elif method == "start_cold_session":
            await client.start_cold_session()
        elif method == "get_roast_state":
            await client.get_roast_state()
        elif method == "mark_beans_added":
            await client.mark_beans_added()
        else:
            await client.finalise_session("session-id")

    assert len(caller.calls) == 1
    assert caller.calls[0][0] == tool
    assert str(raised.value) == "MCP transport failed in cold mode"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)
    assert marker not in "".join(traceback.format_exception(raised.type, raised.value, raised.tb))


@pytest.mark.asyncio
async def test_start_cold_session_requires_confirmed_purpose() -> None:
    """G3: a defaulted or wrong session purpose cannot enter cold mode."""
    start_payload = json.loads((_MCP_TOOL_FIXTURES / "start_roast_session.json").read_text())
    start_payload["session"]["session_purpose"] = "roast"
    client, caller = _unstarted_client_for(start_payload)

    with pytest.raises(ColdSessionPurposeError):
        await client.start_cold_session()
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]
    with pytest.raises(ColdSessionIdentityError):
        await client.get_roast_state()
    with pytest.raises(ColdSessionIdentityError):
        await client.mark_beans_added()
    with pytest.raises(ColdSessionIdentityError):
        await client.finalise_session("session-id")
    assert len(caller.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["", "   "])
async def test_start_cold_session_rejects_blank_identity_and_leaves_stateful_calls_blocked(
    session_id: str,
) -> None:
    """A blank cold-session identity cannot establish observation or activation access."""
    payload = _cold_start_payload()
    cast("dict[str, object]", payload["session"])["session_id"] = session_id
    client, caller = _unstarted_client_for(payload)

    with pytest.raises(ColdSessionIdentityError, match="MCP did not return a cold session id"):
        await client.start_cold_session()
    with pytest.raises(ColdSessionIdentityError):
        await client.get_roast_state()
    with pytest.raises(ColdSessionIdentityError):
        await client.mark_beans_added()
    with pytest.raises(ColdSessionIdentityError):
        await client.finalise_session("session-id")
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["", "   "])
async def test_finalise_session_refuses_explicit_blank_identity_before_transport(
    session_id: str,
) -> None:
    """Explicit blank finalisation identifiers cannot reach the cold transport."""
    client, caller = _client_for(_payload())
    client._cold_session_id = session_id  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ColdSessionIdentityError, match="requested cold session is not established"):
        await client.finalise_session(session_id)
    assert caller.calls == []


@pytest.mark.asyncio
async def test_start_cold_session_maps_malformed_response_without_leakage() -> None:
    """Malformed cold-start responses stay inside the fixed validation boundary."""
    client, caller = _unstarted_client_for({"secret": "payload-marker"})
    with pytest.raises(ColdMcpValidationError) as raised:
        await client.start_cold_session()
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "payload-marker" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("start_payload", [{"secret": "payload-marker"}, None])
async def test_failed_cold_start_leaves_all_stateful_calls_locally_blocked(
    start_payload: object,
) -> None:
    """A failed cold start cannot leave a usable session identity behind."""
    client, caller = _unstarted_client_for(start_payload)
    with pytest.raises(ColdMcpError):
        await client.start_cold_session()
    assert len(caller.calls) == 1
    with pytest.raises(ColdSessionIdentityError):
        await client.get_roast_state()
    with pytest.raises(ColdSessionIdentityError):
        await client.mark_beans_added()
    with pytest.raises(ColdSessionIdentityError):
        await client.finalise_session("session-id")
    assert len(caller.calls) == 1


@pytest.mark.asyncio
async def test_second_cold_start_is_refused_before_transport() -> None:
    """An established cold session cannot be replaced by a second start call."""
    caller = _MappingCaller({"start_roast_session": _cold_start_payload()})
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    with pytest.raises(ColdSessionIdentityError, match="cold session is already established"):
        await client.start_cold_session()
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]


@pytest.mark.asyncio
async def test_unstarted_finalisation_is_refused_without_transport() -> None:
    """An unstarted client cannot issue a finalisation request."""
    client, caller = _unstarted_client_for(_payload())
    with pytest.raises(ColdSessionIdentityError):
        await client.finalise_session("session-id")
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "fixture"),
    [
        ("get_server_info", "get_server_info.json"),
        ("get_runtime_config", "get_runtime_config.json"),
    ],
)
async def test_cold_readonly_methods_return_validated_results(method: str, fixture: str) -> None:
    """The two stateless cold reads return their respective validated mirrors."""
    payload = json.loads((_MCP_TOOL_FIXTURES / fixture).read_text())
    client, caller = _client_for(payload)
    if method == "get_server_info":
        result = await client.get_server_info()
    else:
        result = await client.get_runtime_config()
    assert result is not None
    assert caller.calls == [(method, {})]


@pytest.mark.asyncio
async def test_cold_state_and_activation_return_validated_results() -> None:
    """Established cold state and beans-added activation calls both succeed."""
    caller = _MappingCaller(
        {
            "start_roast_session": _cold_start_payload(),
            "get_roast_state": _cold_state_payload(),
            "mark_beans_added": _cold_marked_payload(),
        }
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    assert (await client.get_roast_state()).state.session_id == "session-id"
    assert (await client.mark_beans_added()).event.kind == "beans_added"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_roast_state", "mark_beans_added"])
async def test_cold_stateful_methods_reject_pre_start_without_transport(method: str) -> None:
    """Stateful cold reads and activation require a started cold session."""
    client, caller = _client_for({})
    client._cold_session_id = None  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ColdSessionIdentityError):
        if method == "get_roast_state":
            await client.get_roast_state()
        else:
            await client.mark_beans_added()
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("get_server_info", {"secret": "payload-marker"}),
        ("get_runtime_config", {"secret": "payload-marker"}),
    ],
)
async def test_stateless_cold_methods_map_malformed_payloads_without_leakage(
    method: str, payload: dict[str, object]
) -> None:
    """Malformed stateless responses become fixed typed cold-boundary errors."""
    client, _ = _client_for(payload)
    with pytest.raises(ColdMcpValidationError) as raised:
        if method == "get_server_info":
            await client.get_server_info()
        else:
            await client.get_runtime_config()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "payload-marker" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "other-session", ColdSessionIdentityError),
        ("session_purpose", "roast", ColdSessionPurposeError),
    ],
)
async def test_finalisation_requires_requested_cold_session_identity(
    field: str, value: str, error: type[ColdMcpError]
) -> None:
    """Finalisation must not accept a clean response for another session or purpose."""
    payload = _payload()
    payload[field] = value
    client, _ = _client_for(payload)

    with pytest.raises(error) as raised:
        await client.finalise_session("session-id")
    assert value not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "other-session", ColdSessionIdentityError),
        ("phase", "pre_roast", ColdSessionPhaseError),
    ],
)
async def test_mark_beans_added_requires_established_cold_session_and_activation_phase(
    field: str, value: str, error: type[ColdMcpError]
) -> None:
    """The inference activation event is bound to the started cold session."""
    session = _cold_start_payload()
    session_state = cast("dict[str, object]", session["session"])
    expected_session_id = cast(str, session_state["session_id"])
    marked = cast(
        "dict[str, object]",
        json.loads((_MCP_TOOL_FIXTURES / "mark_beans_added.json").read_text()),
    )
    marked["session_id"] = expected_session_id
    marked[field] = value
    caller = _MappingCaller({"start_roast_session": session, "mark_beans_added": marked})
    client = ColdCharacterisationMCPClient(caller)

    await client.start_cold_session()
    with pytest.raises(error) as raised:
        await client.mark_beans_added()
    assert value not in str(raised.value)
    assert caller.calls == [
        ("start_roast_session", {"purpose": "cold_characterisation"}),
        ("mark_beans_added", {}),
    ]
    assert expected_session_id


@pytest.mark.asyncio
async def test_mark_beans_added_requires_the_beans_added_event_kind() -> None:
    """The permitted activation command cannot return another event kind."""
    marked = _cold_marked_payload()
    event = cast("dict[str, object]", marked["event"])
    event["kind"] = "first_crack_detected"
    caller = _MappingCaller(
        {"start_roast_session": _cold_start_payload(), "mark_beans_added": marked}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    with pytest.raises(
        ColdSessionPhaseError, match="MCP did not confirm cold inference activation"
    ):
        await client.mark_beans_added()


@pytest.mark.asyncio
async def test_real_mock_finalisation_is_accepted_as_non_streaming() -> None:
    """T-R4a: captured MCP 0.2.1 mock evidence takes only the typed false branch."""
    client, caller = _client_for(_payload())
    result = await client.finalise_session("session-id")

    assert result.status == "clean"
    assert result.final_driver_evidence is not None
    assert result.final_driver_evidence.evidence is not None
    assert result.final_driver_evidence.evidence.command_streaming_required is False
    assert caller.calls == [
        ("finalise_cold_characterisation_session", {"session_id": "session-id"})
    ]
    with pytest.raises(ColdSessionIdentityError):
        await client.mark_beans_added()
    assert len(caller.calls) == 1


@pytest.mark.asyncio
async def test_finalisation_requires_the_started_cold_session_before_transport() -> None:
    """An unstarted or mismatched finalisation request cannot reach MCP."""
    client, caller = _client_for(_payload())
    with pytest.raises(ColdSessionIdentityError, match="requested cold session is not established"):
        await client.finalise_session("other-session")
    assert caller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expected_error", "identity_cleared"),
    [
        ("clean", None, True),
        ("purpose", ColdSessionPurposeError, True),
        ("not_clean", ColdFinalisationNotCleanError, True),
        ("safe_zero", ColdFinalisationSafetyError, True),
        ("capability", ColdFinalisationSafetyError, True),
        ("disconnect", ColdFinalisationSafetyError, True),
        ("malformed", ColdMcpValidationError, False),
        ("wrong_session", ColdSessionIdentityError, False),
    ],
)
async def test_finalisation_resets_identity_only_after_a_bound_terminal_result(
    disposition: str, expected_error: type[Exception] | None, identity_cleared: bool
) -> None:
    """Only a parsed result bound to the established session terminates it locally."""
    payload = _payload()
    if disposition == "purpose":
        payload["session_purpose"] = "roast"
    elif disposition == "not_clean":
        payload["status"] = "partial"
        payload["clean"] = False
    elif disposition == "safe_zero":
        _driver(payload)["safe_zero"] = False
    elif disposition == "capability":
        _driver(payload)["command_loop_running"] = True
    elif disposition == "disconnect":
        cast("dict[str, object]", payload["disconnect"])["last_error"] = "failed"
    elif disposition == "malformed":
        payload["clean"] = "invalid"
    elif disposition == "wrong_session":
        payload["session_id"] = "other-session"
    client, caller = _client_for(payload)

    if expected_error is None:
        assert (await client.finalise_session("session-id")).status == "clean"
    else:
        with pytest.raises(expected_error):
            await client.finalise_session("session-id")

    assert (client._cold_session_id is None) is identity_cleared  # pyright: ignore[reportPrivateUsage]
    if identity_cleared:
        with pytest.raises(ColdSessionIdentityError):
            await client.get_roast_state()
        assert len(caller.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "other-session", ColdSessionIdentityError),
        ("session_purpose", "roast", ColdSessionPurposeError),
    ],
)
async def test_get_roast_state_requires_the_established_cold_session(
    field: str, value: str, error: type[ColdMcpError]
) -> None:
    """Cold state results cannot be accepted from another session or purpose."""
    state = _cold_state_payload()
    state[field] = value
    caller = _MappingCaller(
        {"start_roast_session": _cold_start_payload(), "get_roast_state": state}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()

    with pytest.raises(error) as raised:
        await client.get_roast_state()
    assert value not in str(raised.value)


@pytest.mark.asyncio
async def test_get_roast_state_rejects_a_request_for_a_different_session_before_transport() -> None:
    """A caller cannot redirect the cold state read to another session."""
    caller = _MappingCaller({"start_roast_session": _cold_start_payload()})
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    with pytest.raises(ColdSessionIdentityError, match="requested cold session is not established"):
        await client.get_roast_state("other-session")
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_roast_state", "mark_beans_added"])
async def test_stateful_cold_methods_map_malformed_payloads_without_leakage(method: str) -> None:
    """Malformed stateful responses use the same fixed cold validation boundary."""
    caller = _MappingCaller(
        {"start_roast_session": _cold_start_payload(), method: {"secret": "payload-marker"}}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    with pytest.raises(ColdMcpValidationError) as raised:
        if method == "get_roast_state":
            await client.get_roast_state()
        else:
            await client.mark_beans_added()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "payload-marker" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


@pytest.mark.asyncio
async def test_streaming_finalisation_requires_counters_and_confirmations() -> None:
    """T-R4b: production-streaming evidence is stricter than the mock path."""
    client, _ = _client_for(_streaming_payload())
    assert (await client.finalise_session("session-id")).status == "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", _COUNTERS)
async def test_streaming_finalisation_rejects_each_null_counter(counter: str) -> None:
    """G7c: no retained streaming counter may be null."""
    payload = _streaming_payload()
    _driver(payload)[counter] = None
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", ["command_loop_stopped", "serial_closed"])
async def test_streaming_finalisation_rejects_not_applicable_confirmation(
    confirmation: str,
) -> None:
    """G7c: a streaming driver may not use a non-streaming confirmation."""
    payload = _streaming_payload()
    cast("dict[str, object]", payload["disconnect"])[confirmation] = "not_applicable"
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("confirmation", ["command_loop_stopped", "serial_closed"])
async def test_both_capabilities_reject_not_confirmed(streaming: bool, confirmation: str) -> None:
    """G7c/G14: an unconfirmed disconnect is unsafe in either branch."""
    payload = _streaming_payload() if streaming else _payload()
    cast("dict[str, object]", payload["disconnect"])[confirmation] = "not_confirmed"
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("dimension", _DIMENSIONS)
async def test_both_capabilities_reject_nonzero_six_dimension_evidence(
    streaming: bool, dimension: str
) -> None:
    """G7b: capability never relaxes any command dimension."""
    payload = _streaming_payload() if streaming else _payload()
    is_boolean_dimension = dimension.endswith("_on") or dimension == "solenoid_open"
    _driver(payload)[dimension] = True if is_boolean_dimension else 1
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("safe_zero", False),
        ("non_zero_dimensions", ["heat_level_percent"]),
        ("connected", True),
    ],
)
async def test_finalisation_requires_unconditional_safe_driver_evidence(
    field: str, value: object
) -> None:
    """G7b: AC23 never relaxes safe-zero, dimensions, or disconnect state."""
    payload = _payload()
    _driver(payload)[field] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
async def test_finalisation_requires_driver_evidence_for_every_capability() -> None:
    """G7b: schema-optional driver evidence is operationally mandatory."""
    payload = _payload()
    payload["final_driver_evidence"] = None
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


def test_capability_evidence_locally_requires_a_read_outcome() -> None:
    """The capability predicate cannot rely on an earlier safe-zero evaluation."""
    payload = _payload()
    final_read = cast("dict[str, object]", payload["final_driver_evidence"])
    final_read["outcome"] = "unsupported"
    result = SessionFinalisationResult.model_validate(payload)
    assert _finalisation_has_capability_compatible_evidence(result) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("field", ["command_loop_running", "serial_open"])
async def test_both_capabilities_reject_retained_active_driver_state(
    streaming: bool, field: str
) -> None:
    """A final affirmative loop or serial state contradicts clean finalisation."""
    payload = _streaming_payload() if streaming else _payload()
    _driver(payload)[field] = True
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.parametrize("value", [None, "false", 0])
def test_capability_is_required_and_strictly_boolean(value: object) -> None:
    """T-G4c: capability cannot default or coerce into the mock branch."""
    payload = _payload()
    _driver(payload)["command_streaming_required"] = value
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


def test_counter_keys_are_required_even_when_non_streaming() -> None:
    """T-G4e: non-streaming permits null values, never absent counter keys."""
    payload = _payload()
    del _driver(payload)["command_write_count"]
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


@pytest.mark.asyncio
async def test_mock_like_driver_name_cannot_select_non_streaming_rules() -> None:
    """T-G7c5: only typed capability, never a driver label, selects the branch."""
    payload = _streaming_payload()
    _driver(payload)["driver"] = "mock-like-name"
    _driver(payload)["command_send_attempts"] = None
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
async def test_disconnect_attempt_must_complete_without_error() -> None:
    """G14: the capability exception never waives the actual disconnect."""
    payload = _payload()
    disconnect = cast("dict[str, object]", payload["disconnect"])
    disconnect["last_error"] = "disconnect failed"
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_count", 0),
        ("first_attempted_at_utc", None),
        ("first_attempted_at_utc", ""),
        ("first_attempted_at_utc", "   "),
        ("last_attempted_at_utc", None),
        ("last_attempted_at_utc", ""),
        ("last_attempted_at_utc", "   "),
        ("last_returned_without_error", False),
        ("last_error", "disconnect failure"),
        ("connected_false_confirmed", False),
    ],
)
async def test_each_disconnect_predicate_independently_fails_closed(
    field: str, value: object
) -> None:
    """Every required disconnect predicate independently rejects finalisation."""
    payload = _payload()
    cast("dict[str, object]", payload["disconnect"])[field] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError, match="clean disconnect evidence"):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(RejectionReason))
async def test_every_closed_rejection_reason_cannot_be_clean(reason: RejectionReason) -> None:
    """G5: every accepted closed rejection value rejects the finalisation."""
    payload = _payload()
    payload["rejection_reason"] = reason.value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError) as raised:
        await client.finalise_session("session-id")
    assert raised.value.result.rejection_reason is reason
    with pytest.raises(ColdSessionIdentityError):
        await client.get_roast_state()


def test_closed_rejection_reason_grammar_is_exact_and_json_client_round_trips() -> None:
    """The strict client accepts exactly the twelve ratified rejection values."""
    assert {reason.value for reason in RejectionReason} == {
        "unknown_session",
        "not_latest_session",
        "session_not_active",
        "session_purpose_not_eligible",
        "session_faulted",
        "command_in_progress",
        "finalisation_in_progress",
        "driver_lifecycle_evidence_unsupported",
        "driver_state_unreadable",
        "driver_state_malformed",
        "driver_not_connected",
        "driver_state_not_safe_zero",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unknown_session", "session_purpose_not_eligible"])
async def test_rejected_finalisation_retains_evidence_before_purpose_check(reason: str) -> None:
    """Real rejection shapes remain finalisation evidence even without cold purpose."""
    payload = _payload()
    payload["status"] = "rejected"
    payload["clean"] = False
    payload["rejection_reason"] = reason
    payload["session_purpose"] = None
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError) as raised:
        await client.finalise_session("session-id")
    assert raised.value.result.rejection_reason is RejectionReason(reason)


@pytest.mark.asyncio
async def test_rejected_finalisation_for_another_session_is_not_attributed_to_cold_session() -> (
    None
):
    """A rejected response must bind its session identifier before retaining evidence."""
    payload = _payload()
    payload["session_id"] = "other-session"
    payload["status"] = "rejected"
    payload["clean"] = False
    payload["rejection_reason"] = "unknown_session"
    payload["session_purpose"] = None
    client, _ = _client_for(payload)
    with pytest.raises(
        ColdSessionIdentityError, match="MCP did not return the requested cold session"
    ):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "failures",
            [
                {
                    "stage": "recording",
                    "code": "failure",
                    "message": "private-evidence-marker",
                    "attempt_number": 1,
                    "recorded_at_utc": "2026-01-01T00:00:00+00:00",
                }
            ],
        ),
        ("session_active_after", True),
    ],
)
async def test_claimed_clean_finalisation_rejects_failures_and_active_session(
    field: str, value: object
) -> None:
    """A clean status requires no retained failures and a stopped session."""
    payload = _payload()
    payload[field] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError) as raised:
        await client.finalise_session("session-id")
    assert raised.value.result.session_id == "session-id"
    assert "session-id" not in repr(raised.value)
    assert "private-evidence-marker" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index", "status"),
    [(0, "not_applicable"), (3, "not_applicable"), (1, "failed"), (2, "incomplete")],
)
async def test_claimed_clean_finalisation_rejects_inconsistent_stage_status(
    index: int, status: str
) -> None:
    """Mandatory stages complete; optional stages only complete or do not apply."""
    payload = _payload()
    stages = cast("list[dict[str, object]]", payload["stages"])
    stages[index]["status"] = status
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("emergency_stop_ordering", "emergency_stop_after_disconnect_attempt"),
        ("session_phase_after", "complete"),
    ],
)
async def test_claimed_clean_finalisation_rejects_terminal_ordering_and_phase(
    field: str, value: str
) -> None:
    """Clean finalisation retains only the cold-safe ordering and post-phase grammar."""
    payload = _payload()
    payload[field] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("sampler", ColdFinalisationNotCleanError),
        ("pre_finalisation_first_crack_status", ColdFinalisationNotCleanError),
        ("first_crack_runtime", ColdFinalisationNotCleanError),
        ("recording", ColdFinalisationNotCleanError),
        ("final_driver_evidence", ColdFinalisationSafetyError),
    ],
)
async def test_clean_finalisation_requires_every_optional_evidence_member(
    streaming: bool, field: str, error: type[ColdMcpError]
) -> None:
    """G7b requires every retained cold finalisation evidence member in both branches."""
    payload = _streaming_payload() if streaming else _payload()
    payload[field] = None
    client, _ = _client_for(payload)
    with pytest.raises(error):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audio_running", "accepted"), [(None, False), (False, False), (True, False)]
)
async def test_not_applicable_first_crack_requires_inactive_audio(
    audio_running: bool | None, accepted: bool
) -> None:
    """Not-applicable first-crack teardown requires retained runtime evidence."""
    payload = _payload()
    payload["first_crack_runtime"] = None
    if audio_running is None:
        payload["pre_finalisation_first_crack_status"] = None
    else:
        pre_status = cast("dict[str, object]", payload["pre_finalisation_first_crack_status"])
        pre_status["audio_running"] = audio_running
    client, _ = _client_for(payload)
    if accepted:
        assert (await client.finalise_session("session-id")).status == "clean"
    else:
        with pytest.raises(ColdFinalisationNotCleanError):
            await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_outcome", ["not_active", "stopped"])
@pytest.mark.parametrize("audio_running", [False, True])
async def test_first_crack_runtime_success_requires_stopped_audio(
    runtime_outcome: str, audio_running: bool
) -> None:
    """Returned first-crack runtime success cannot contradict its final audio status."""
    payload = _payload()
    runtime = cast("dict[str, object]", payload["first_crack_runtime"])
    runtime["outcome"] = runtime_outcome
    final_status = cast("dict[str, object]", runtime["final_status"])
    final_status["audio_running"] = audio_running
    if runtime_outcome == "stopped":
        cast("list[dict[str, object]]", payload["stages"])[1]["status"] = "completed"
    client, _ = _client_for(payload)
    if audio_running:
        with pytest.raises(ColdFinalisationNotCleanError):
            await client.finalise_session("session-id")
    else:
        assert (await client.finalise_session("session-id")).status == "clean"


@pytest.mark.asyncio
async def test_successful_first_crack_stop_rejects_a_stop_error() -> None:
    """A stopped first-crack runtime cannot retain a stop failure diagnostic."""
    payload = _payload()
    cast("list[dict[str, object]]", payload["stages"])[1]["status"] = "completed"
    runtime = cast("dict[str, object]", payload["first_crack_runtime"])
    runtime["outcome"] = "stopped"
    runtime["stop_error"] = "reader-stop-failed"
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
async def test_not_active_first_crack_runtime_rejects_a_stop_error() -> None:
    """A not-active first-crack runtime cannot retain a stop failure diagnostic."""
    payload = _payload()
    cast("dict[str, object]", payload["first_crack_runtime"])["stop_error"] = "reader-stop-failed"
    result = SessionFinalisationResult.model_validate(payload)
    assert finalisation_is_clean(result) is False
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [False, True])
async def test_not_applicable_recording_requires_no_expected_recording(expected: bool) -> None:
    """Not-configured recording is clean only when recording was not expected."""
    payload = _payload()
    recording = cast("dict[str, object]", payload["recording"])
    recording["expected"] = expected
    client, _ = _client_for(payload)
    if expected:
        with pytest.raises(ColdFinalisationNotCleanError):
            await client.finalise_session("session-id")
    else:
        assert (await client.finalise_session("session-id")).status == "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "recording-finalise-failed"])
async def test_finalised_recording_rejects_a_retained_reason(reason: str | None) -> None:
    """A finalised recording outcome cannot retain an error reason."""
    payload = _payload()
    cast("list[dict[str, object]]", payload["stages"])[2]["status"] = "completed"
    recording = cast("dict[str, object]", payload["recording"])
    recording["expected"] = True
    recording["outcome"] = "finalised"
    recording["reason"] = reason
    client, _ = _client_for(payload)
    if reason is None:
        assert (await client.finalise_session("session-id")).status == "clean"
    else:
        with pytest.raises(ColdFinalisationNotCleanError):
            await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "driver-read-failed"])
async def test_final_driver_read_requires_no_error(error: str | None) -> None:
    """A nominal read with retained read error is not trusted safety evidence."""
    payload = _payload()
    cast("dict[str, object]", payload["final_driver_evidence"])["error"] = error
    client, _ = _client_for(payload)
    if error is None:
        assert (await client.finalise_session("session-id")).status == "clean"
    else:
        with pytest.raises(ColdFinalisationSafetyError, match="safe-zero evidence"):
            await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_loop_stopped", "serial_closed"),
    [("confirmed", "confirmed"), ("confirmed", "not_applicable")],
)
async def test_non_streaming_finalisation_accepts_ratified_confirmations(
    command_loop_stopped: str, serial_closed: str
) -> None:
    """The non-streaming branch accepts exactly its ratified confirmation combinations."""
    payload = _payload()
    disconnect = cast("dict[str, object]", payload["disconnect"])
    disconnect["command_loop_stopped"] = command_loop_stopped
    disconnect["serial_closed"] = serial_closed
    client, _ = _client_for(payload)
    assert (await client.finalise_session("session-id")).status == "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("emergency_stop_ordering", "not_reached", True),
        ("emergency_stop_ordering", "finalisation_committed_first", True),
        ("emergency_stop_ordering", "emergency_stop_before_disconnect_commit", False),
        ("emergency_stop_ordering", "emergency_stop_after_disconnect_attempt", False),
        ("session_phase_after", "pre_roast", True),
        ("session_phase_after", "roasting", True),
        ("session_phase_after", "development", False),
        ("session_phase_after", "dropped", False),
        ("session_phase_after", "cooling", False),
        ("session_phase_after", "complete", False),
        ("session_phase_after", "fault", False),
    ],
)
async def test_claimed_clean_finalisation_accepts_only_safe_ordering_and_phase(
    field: str, value: str, accepted: bool
) -> None:
    """All admitted clean ordering and phase values are exercised at the client gate."""
    payload = _payload()
    payload[field] = value
    client, _ = _client_for(payload)
    if accepted:
        assert (await client.finalise_session("session-id")).status == "clean"
    else:
        with pytest.raises(ColdFinalisationNotCleanError):
            await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unsupported", "unreadable", "malformed"])
async def test_driver_read_outcomes_fail_the_unconditional_safe_zero_gate(outcome: str) -> None:
    """Non-read driver outcomes cannot bypass the unconditional safe-zero gate."""
    payload = _payload()
    final_read = cast("dict[str, object]", payload["final_driver_evidence"])
    final_read["outcome"] = outcome
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError, match="safe-zero evidence"):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
async def test_driver_read_without_evidence_fails_the_unconditional_safe_zero_gate() -> None:
    """A read outcome without its retained state evidence is not safe-zero proof."""
    payload = _payload()
    cast("dict[str, object]", payload["final_driver_evidence"])["evidence"] = None
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError, match="safe-zero evidence"):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_index", "stage_status", "evidence_field", "evidence_value"),
    [
        (1, "completed", "outcome", "not_active"),
        (1, "not_applicable", "outcome", "stopped"),
        (2, "completed", "outcome", "not_configured"),
        (2, "not_applicable", "outcome", "finalised"),
    ],
)
async def test_claimed_clean_finalisation_requires_stage_evidence_consistency(
    stage_index: int, stage_status: str, evidence_field: str, evidence_value: str
) -> None:
    """First-crack and recording stage statuses agree with their retained evidence."""
    payload = _payload()
    stages = cast("list[dict[str, object]]", payload["stages"])
    stages[stage_index]["status"] = stage_status
    section = "first_crack_runtime" if stage_index == 1 else "recording"
    evidence = cast("dict[str, object]", payload[section])
    evidence[evidence_field] = evidence_value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.parametrize("bad_reason", ["driver_state_unknown", "", 0])
def test_unknown_rejection_reason_is_not_degraded(bad_reason: object) -> None:
    """G4: non-closed rejection values fail at the strict mirror boundary."""
    payload = _payload()
    payload["rejection_reason"] = bad_reason
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


@pytest.mark.parametrize(
    ("status", "clean", "abort_reason"),
    [
        ("clean", False, None),
        ("partial", True, None),
        ("clean", True, "session_or_reservation_changed"),
    ],
)
def test_clean_gate_requires_all_four_fields(
    status: str, clean: bool, abort_reason: str | None
) -> None:
    """G5: status alone is insufficient for a clean finalisation."""
    payload = _payload()
    payload["status"] = status
    payload["clean"] = clean
    payload["abort_reason"] = abort_reason
    assert (
        finalisation_is_clean(SessionFinalisationResult.model_validate_json(json.dumps(payload)))
        is False
    )


def test_strict_mirror_rejects_extra_field_and_bad_confirmation() -> None:
    """T-G4/T-G4d: strict extras and confirmation literals cannot drift."""
    payload = _payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)

    payload = _payload()
    cast("dict[str, object]", payload["disconnect"])["serial_closed"] = "skipped"
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("clean",), "true"),
        (("attempt_number",), "1"),
        (("final_driver_evidence", "evidence", "heat_level_percent"), "0"),
        (("final_driver_evidence", "evidence", "connected"), "false"),
    ],
)
def test_strict_finalisation_mirror_rejects_scalar_coercion(
    path: tuple[str, ...], value: object
) -> None:
    """Strict finalisation mirrors reject top-level and nested scalar coercion."""
    payload = _payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        target = cast("dict[str, object]", target[key])
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


@pytest.mark.parametrize("stage_count", [0, 3])
def test_finalisation_requires_exactly_four_ordered_stages(stage_count: int) -> None:
    """The finalisation stage tuple requires all four ordered stage records."""
    payload = _payload()
    payload["stages"] = cast("list[object]", payload["stages"])[:stage_count]
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


def test_finalisation_rejects_out_of_order_four_stage_sequence() -> None:
    """The four-stage validator rejects even a complete sequence in the wrong order."""
    payload = _payload()
    stages = cast("list[object]", payload["stages"])
    stages[0], stages[1] = stages[1], stages[0]
    with pytest.raises(ValidationError, match="four-stage order"):
        SessionFinalisationResult.model_validate_json(json.dumps(payload))


def test_finalisation_rejects_unknown_post_finalisation_phase() -> None:
    """The mirror follows the installed MCP 0.2.1 roast-phase grammar exactly."""
    payload = _payload()
    payload["session_phase_after"] = "unknown_phase"
    with pytest.raises(ValidationError):
        SessionFinalisationResult.model_validate(payload)


@pytest.mark.asyncio
async def test_client_maps_malformed_finalisation_to_typed_cold_error() -> None:
    """Client callers receive a fixed typed error without payload-bearing causes."""
    payload = _payload()
    payload["clean"] = "sentinel-secret"
    client, _ = _client_for(payload)
    with pytest.raises(
        ColdMcpValidationError, match="MCP response failed cold contract validation"
    ) as raised:
        await client.finalise_session("session-id")
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "sentinel-secret" not in str(raised.value)
    assert "sentinel-secret" not in repr(raised.value)
    assert "sentinel-secret" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


async def _assert_public_finalisation_validation_failure(
    payload: object, marker: str | None
) -> None:
    """Assert the public JSON client contains one malformed finalisation payload."""
    client, _ = _client_for(payload)
    with pytest.raises(ColdMcpValidationError) as raised:
        await client.finalise_session("session-id")
    assert str(raised.value) == "MCP response failed cold contract validation"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    if marker is not None:
        assert marker not in str(raised.value)
        assert marker not in repr(raised.value)
        assert marker not in "".join(
            traceback.format_exception(raised.type, raised.value, raised.tb)
        )


@pytest.mark.asyncio
async def test_client_rejects_non_mapping_finalisation_and_nested_container_values() -> None:
    """Top-level and nested JSON non-mappings fail through the fixed client boundary."""
    await _assert_public_finalisation_validation_failure(["top-level-marker"], "top-level-marker")

    driver_payload = _payload()
    final_read = cast("dict[str, object]", driver_payload["final_driver_evidence"])
    final_read["evidence"] = ["driver-marker"]
    await _assert_public_finalisation_validation_failure(driver_payload, "driver-marker")

    recording_payload = _payload()
    recording_payload["recording"] = ["recording-marker"]
    await _assert_public_finalisation_validation_failure(recording_payload, "recording-marker")


@pytest.mark.asyncio
@pytest.mark.parametrize("container_field", ["non_zero_dimensions", "artifacts"])
@pytest.mark.parametrize("missing", [False, True])
async def test_client_rejects_missing_or_non_list_immutable_json_containers(
    container_field: str, missing: bool
) -> None:
    """Immutable mirror containers reject missing and non-list JSON representations."""
    marker = f"{container_field}-marker"
    payload = _payload()
    if container_field == "non_zero_dimensions":
        container = _driver(payload)
    else:
        container = cast("dict[str, object]", payload["recording"])
    if missing:
        del container[container_field]
    else:
        container[container_field] = marker
    await _assert_public_finalisation_validation_failure(payload, None if missing else marker)


@pytest.mark.asyncio
@pytest.mark.parametrize("container_field", ["stages", "failures"])
@pytest.mark.parametrize("missing", [False, True])
async def test_client_rejects_missing_or_non_list_finalisation_containers(
    container_field: str, missing: bool
) -> None:
    """The finalisation mirror requires JSON lists for both immutable root containers."""
    marker = f"{container_field}-marker"
    payload = _payload()
    if missing:
        del payload[container_field]
    else:
        payload[container_field] = marker
    await _assert_public_finalisation_validation_failure(payload, None if missing else marker)


_STATUS_FLOAT_FIELDS = (
    "detected_monotonic_seconds",
    "mic_peak_dbfs",
    "mic_rms_dbfs",
    "estimated_lost_audio_ms_last_minute",
    "last_inference_duration_ms",
    "max_inference_duration_ms",
)
_NULLABLE_STATUS_FLOAT_FIELDS = _STATUS_FLOAT_FIELDS[:3]


def _first_crack_status(payload: dict[str, object], location: str) -> dict[str, object]:
    """Return the mutable pre- or final first-crack status mapping of a payload."""
    if location == "pre":
        return cast("dict[str, object]", payload["pre_finalisation_first_crack_status"])
    runtime = cast("dict[str, object]", payload["first_crack_runtime"])
    return cast("dict[str, object]", runtime["final_status"])


async def _assert_status_type_refused(payload: dict[str, object]) -> None:
    """Assert one fixed refusal before identity reset, with exactly one MCP call."""
    await _assert_public_finalisation_validation_failure(payload, None)
    client, caller = _client_for(payload)
    with pytest.raises(ColdMcpValidationError):
        await client.finalise_session("session-id")
    assert caller.calls == [
        ("finalise_cold_characterisation_session", {"session_id": "session-id"})
    ]
    assert client._cold_session_id == "session-id"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["pre", "final"])
@pytest.mark.parametrize("field", _STATUS_FLOAT_FIELDS)
async def test_finalisation_refuses_json_integer_in_status_float_field(
    location: str, field: str
) -> None:
    """T1: a JSON integer in any status float field is refused, never converted."""
    payload = _payload()
    _first_crack_status(payload, location)[field] = 1
    await _assert_status_type_refused(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["pre", "final"])
async def test_finalisation_refuses_value_changing_integer_in_status(location: str) -> None:
    """T2: an integer above 2**53 that would round on conversion is refused."""
    payload = _payload()
    _first_crack_status(payload, location)["max_inference_duration_ms"] = 2**53 + 1
    await _assert_status_type_refused(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["pre", "final"])
async def test_finalisation_refuses_out_of_range_integer_in_status(location: str) -> None:
    """T3: an integer beyond the float range still maps to the fixed error."""
    payload = _payload()
    _first_crack_status(payload, location)["max_inference_duration_ms"] = 10**400
    await _assert_status_type_refused(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["pre", "final"])
async def test_finalisation_accepts_exact_float_and_null_status_values(location: str) -> None:
    """T4: exact floats and JSON nulls in status float fields remain accepted."""
    payload = _payload()
    status = _first_crack_status(payload, location)
    status["max_inference_duration_ms"] = 1.5
    for field in _NULLABLE_STATUS_FLOAT_FIELDS:
        status[field] = None
    client, _ = _client_for(payload)
    result = await client.finalise_session("session-id")
    assert result.status == "clean"
    if location == "pre":
        parsed = result.pre_finalisation_first_crack_status
    else:
        assert result.first_crack_runtime is not None
        parsed = result.first_crack_runtime.final_status
    assert parsed is not None
    assert type(parsed.max_inference_duration_ms) is float
    assert parsed.max_inference_duration_ms == 1.5
    for field in _NULLABLE_STATUS_FLOAT_FIELDS:
        assert getattr(parsed, field) is None


@pytest.mark.asyncio
async def test_finalisation_status_type_check_precedes_session_identity_check() -> None:
    """T5: the exact-type refusal happens before the returned session id comparison."""
    mismatched = _payload()
    mismatched["session_id"] = "other-session"
    client, _ = _client_for(mismatched)
    with pytest.raises(ColdSessionIdentityError):
        await client.finalise_session("session-id")

    payload = _payload()
    payload["session_id"] = "other-session"
    _first_crack_status(payload, "final")["max_inference_duration_ms"] = 1
    await _assert_status_type_refused(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"bad": {1, 2}}, {"cycle": None}])
async def test_client_maps_serialisation_failures_to_fixed_typed_cold_error(
    payload: dict[str, object],
) -> None:
    """JSON type and cyclic failures are contained at the cold client boundary."""
    if "cycle" in payload:
        payload["cycle"] = payload
    client, _ = _client_for(payload)
    with pytest.raises(ColdMcpValidationError) as raised:
        await client.finalise_session("session-id")
    assert str(raised.value) == "MCP response failed cold contract validation"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_client_rejects_non_finite_json_values(value: float) -> None:
    """Strict JSON serialization rejects non-finite numeric payload values."""
    payload = _payload()
    payload["first_started_session_elapsed_seconds"] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdMcpValidationError) as raised:
        await client.finalise_session("session-id")
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("sampler", "thread_alive_after_join", True),
        ("sampler", "last_error", "reader failure"),
        ("first_crack_runtime", "capture_running_after_stop", True),
        ("first_crack_runtime", "outcome", "capture_still_running"),
        ("first_crack_runtime", "outcome", "stop_failed"),
    ],
)
async def test_claimed_clean_finalisation_rejects_contradictory_shutdown_evidence(
    section: str, field: str, value: object
) -> None:
    """Read teardown evidence cannot contradict a claimed-clean finalisation."""
    payload = _payload()
    evidence = cast("dict[str, object]", payload[section])
    evidence[field] = value
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationNotCleanError):
        await client.finalise_session("session-id")


@pytest.mark.asyncio
async def test_finalisation_errors_retain_validated_evidence_without_rendering_it() -> None:
    """Safety failures keep parsed evidence private while exposing a fixed public message."""
    payload = _payload()
    _driver(payload)["driver"] = "private-evidence-marker"
    _driver(payload)["safe_zero"] = False
    client, _ = _client_for(payload)
    with pytest.raises(ColdFinalisationSafetyError) as raised:
        await client.finalise_session("session-id")
    assert raised.value.result.session_id == "session-id"
    assert str(raised.value) == "MCP finalisation lacks safe-zero evidence"
    assert "session-id" not in repr(raised.value)
    assert "private-evidence-marker" not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )


def test_capability_branch_has_one_predicate_and_no_driver_name_path() -> None:
    """Class H: source shape precludes a second or name-derived branch."""
    source = Path(inspect.getfile(ColdCharacterisationMCPClient)).read_text()
    assert source.count("if _command_streaming_required(evidence):") == 1
    assert "driver_name" not in source
    assert "driver_type" not in source


def _observation(payload: dict[str, object]) -> bool | None:
    """Return the H-A accessor's reading of one strictly parsed payload."""
    return finalisation_command_streaming_observation(
        SessionFinalisationResult.model_validate_json(json.dumps(payload))
    )


def test_streaming_observation_reads_trusted_final_evidence_only() -> None:
    """H-A: the accessor returns the predicate value, or ``None`` without trusted evidence."""
    assert _observation(_payload()) is False
    assert _observation(_streaming_payload()) is True
    absent = _streaming_payload()
    absent["final_driver_evidence"] = None
    assert _observation(absent) is None
    for update in (
        {"outcome": "unreadable", "error": "driver_state_unreadable", "evidence": None},
        {"outcome": "read", "error": "late error"},
        {"outcome": "read", "error": None, "evidence": None},
        {"outcome": "unsupported"},
    ):
        untrusted = _streaming_payload()
        final = cast("dict[str, object]", untrusted["final_driver_evidence"])
        final.update(update)
        assert _observation(untrusted) is None, update


def test_streaming_observation_delegates_to_the_sole_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-A: the accessor reads the capability only through the single predicate."""
    seen: list[bool] = []

    def spy(evidence: object) -> bool:
        seen.append(True)
        return True

    monkeypatch.setattr(cold_mcp, "_command_streaming_required", spy)
    assert _observation(_payload()) is True
    assert seen == [True]
    source = inspect.getsource(finalisation_command_streaming_observation)
    assert "return _command_streaming_required(evidence)" in source
    assert ".command_streaming_required" not in source


# --- #954 slice 4a: strict per-tick first-crack observation (HD-1) ---

#: Audio fields the tolerant ``FirstCrackStatus`` mirror requires (no default).
_REQUIRED_MIRROR_AUDIO_FIELDS: tuple[str, ...] = (
    "mode",
    "status",
    "detected_at_utc",
    "detected_monotonic_seconds",
    "allow_manual_override",
)
#: Audio fields the tolerant mirror defaults (nine) or silently ignores (six).
_TOLERATED_AUDIO_FIELDS: tuple[str, ...] = (
    "reason",
    "audio_running",
    "queued_window_count",
    "emitted_window_count",
    "dropped_window_count",
    "processed_window_count",
    "overflow_count_last_minute",
    "estimated_lost_audio_ms_last_minute",
    "total_overflow_count",
    "mic_peak_dbfs",
    "mic_rms_dbfs",
    "max_consecutive_overflow_count",
    "last_inference_duration_ms",
    "max_inference_duration_ms",
    "inference_overrun_count",
)
_TICK_MESSAGE = "MCP tick audio evidence failed strict projection"
_GENERIC_MESSAGE = "MCP response failed cold contract validation"
_COLD_PACKAGE = Path(inspect.getfile(cold_mcp)).parent


def _first_crack(payload: dict[str, object]) -> dict[str, object]:
    """Return the mutable raw first-crack mapping of a tick payload."""
    return cast("dict[str, object]", payload["first_crack_status"])


def _assert_tolerant_mirror_accepts(payload: dict[str, object]) -> None:
    """Prove in-test that the tolerant roast mirror accepts this payload."""
    RoastSessionState.model_validate_json(json.dumps(payload))


def _runtime_canary() -> str:
    """Assemble a synthetic credential-shaped canary at runtime (never a real secret)."""
    return "".join(["sk", "-or-v1-", "c0ld", "canary", "7" * 12])


def _assert_contained(error: BaseException, canary: str) -> None:
    """Assert a canary is absent from every public rendering of an error."""
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in str(error)
    assert canary not in repr(error)
    assert all(canary not in repr(arg) for arg in error.args)
    assert canary not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


async def _started_client(
    state: object,
) -> tuple[ColdCharacterisationMCPClient, _MappingCaller]:
    """Return a cold client established through its public start method."""
    caller = _MappingCaller(
        {"start_roast_session": _cold_start_payload(), "get_roast_state": state}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()
    return client, caller


def _state_calls(caller: _MappingCaller) -> list[tuple[str, dict[str, object]]]:
    """Return only the recorded per-tick state reads."""
    return [call for call in caller.calls if call[0] == "get_roast_state"]


@pytest.mark.asyncio
async def test_cold_tick_returns_strict_audio_from_the_raw_response() -> None:
    """T1/AC-P1: every audio field equals the raw response value, from one read."""
    payload = _cold_state_payload()
    raw_audio = dict(_first_crack(payload))
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    assert type(observation) is ColdTickObservation
    assert type(observation.audio.audio) is ColdTickAudioSample
    for field in ColdAudioField:
        if field is ColdAudioField.UNKNOWN_FIELD:
            continue
        value = getattr(observation.audio.audio, field.value)
        assert value == raw_audio[field.value], field
        assert type(value) is type(raw_audio[field.value]), field
    assert observation.state.session_id == "session-id"
    assert observation.audio.raw_audio_extra == {}
    assert _state_calls(caller) == [("get_roast_state", {"session_id": "session-id"})]


def test_tick_audio_field_partition_is_exact() -> None:
    """T2: required-mirror and tolerated fields partition the closed audio names."""
    required = set(_REQUIRED_MIRROR_AUDIO_FIELDS)
    tolerated = set(_TOLERATED_AUDIO_FIELDS)
    assert len(required) == 5
    assert len(tolerated) == 15
    assert required.isdisjoint(tolerated)
    assert required | tolerated == {
        field.value for field in ColdAudioField if field is not ColdAudioField.UNKNOWN_FIELD
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _REQUIRED_MIRROR_AUDIO_FIELDS)
async def test_missing_required_mirror_audio_field_fails_generic_validation(field: str) -> None:
    """T2a: a field the tolerant mirror requires fails before projection is reachable."""
    payload = _cold_state_payload()
    del _first_crack(payload)[field]
    client, caller = await _started_client(payload)

    with pytest.raises(ColdMcpValidationError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdMcpValidationError
    assert str(raised.value) == _GENERIC_MESSAGE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _TOLERATED_AUDIO_FIELDS)
async def test_missing_tolerated_audio_field_fails_strict_projection(field: str) -> None:
    """T2b/AC-P2: a field the tolerant mirror would default or drop cannot be invented."""
    payload = _cold_state_payload()
    del _first_crack(payload)[field]
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT
    assert raised.value.field_names == (ColdAudioField(field),)
    assert str(raised.value) == _TICK_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("mode", "not-a-mode"), ("status", "not-a-status")])
async def test_out_of_set_mode_or_status_fails_generic_validation(field: str, value: str) -> None:
    """T3a: closed mode/status values are refused by the tolerant mirror first."""
    payload = _cold_state_payload()
    _first_crack(payload)[field] = value
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpValidationError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdMcpValidationError
    assert str(raised.value) == _GENERIC_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_consecutive_overflow_count", "0"),
        ("inference_overrun_count", True),
        ("max_inference_duration_ms", "1.5"),
        ("last_inference_duration_ms", None),
        ("mic_peak_dbfs", "x"),
        ("queued_window_count", "0"),
        ("audio_running", "true"),
        ("estimated_lost_audio_ms_last_minute", "0.0"),
    ],
)
async def test_coerced_audio_value_fails_strict_projection(field: str, value: object) -> None:
    """T3b/AC-P2: a value the tolerant mirror accepts or ignores is never coerced."""
    payload = _cold_state_payload()
    _first_crack(payload)[field] = value
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT
    assert raised.value.field_names == (ColdAudioField(field),)


@pytest.mark.asyncio
async def test_unknown_in_bound_audio_key_is_preserved_losslessly() -> None:
    """T4/AC-P3: a forward-compatible key is kept in extras and fills no named field."""
    payload = _cold_state_payload()
    _first_crack(payload)["future_counter"] = 1
    client, _ = await _started_client(payload)

    observation = await client.get_roast_state()

    assert observation.audio.raw_audio_extra == {"future_counter": 1}
    assert "future_counter" not in type(observation.audio.audio).model_fields
    assert observation.audio.audio.max_consecutive_overflow_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["max_consecutive_overflow", "MAX_CONSECUTIVE_OVERFLOW_COUNT"])
async def test_variant_audio_key_cannot_stand_in_for_the_real_field(variant: str) -> None:
    """T4: a near-miss key name never satisfies the required projection field."""
    payload = _cold_state_payload()
    audio = _first_crack(payload)
    audio[variant] = audio.pop("max_consecutive_overflow_count")
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT
    assert raised.value.field_names == (ColdAudioField.MAX_CONSECUTIVE_OVERFLOW_COUNT,)


@pytest.mark.asyncio
async def test_oversized_audio_extra_fails_strict_projection() -> None:
    """T5: forward-compatible extras stay inside their canonical byte bound."""
    payload = _cold_state_payload()
    _first_crack(payload)["future_blob"] = "x" * (MAX_RAW_AUDIO_EXTRA_BYTES + 1)
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE
    assert raised.value.field_names == ()


@pytest.mark.asyncio
async def test_non_finite_audio_value_fails_generic_validation() -> None:
    """T5: a non-finite float fails serialisation before either parse."""
    payload = _cold_state_payload()
    _first_crack(payload)["max_inference_duration_ms"] = float("nan")
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpValidationError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdMcpValidationError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "other-session", ColdSessionIdentityError),
        ("session_purpose", "roast", ColdSessionPurposeError),
    ],
)
async def test_identity_and_purpose_checks_precede_audio_projection(
    field: str, value: str, error: type[ColdMcpError]
) -> None:
    """T6: a wrong session or purpose wins over incomplete audio evidence."""
    payload = _cold_state_payload()
    payload[field] = value
    del _first_crack(payload)["max_consecutive_overflow_count"]
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is error


@pytest.mark.asyncio
async def test_generic_tick_validation_failure_contains_the_rejected_value() -> None:
    """T7a/AC-P5: a rejected mirror value never reaches the error chain."""
    canary = _runtime_canary()
    payload = _cold_state_payload()
    _first_crack(payload)["mode"] = canary
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpValidationError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdMcpValidationError
    _assert_contained(raised.value, canary)


@pytest.mark.asyncio
async def test_projection_failure_contains_the_rejected_value() -> None:
    """T7b/AC-P5: a value the tolerant mirror ignores never reaches the error chain."""
    canary = _runtime_canary()
    payload = _cold_state_payload()
    _first_crack(payload)["max_consecutive_overflow_count"] = canary
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert isinstance(raised.value, ColdMcpValidationError)
    assert raised.value.failure is ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT
    assert raised.value.field_names == (ColdAudioField.MAX_CONSECUTIVE_OVERFLOW_COUNT,)
    assert raised.value.args == (_TICK_MESSAGE,)
    _assert_contained(raised.value, canary)


@pytest.mark.asyncio
async def test_projection_failure_contains_a_rejected_key_name() -> None:
    """T7c/AC-P5: an over-length key name is refused without being rendered."""
    canary = _runtime_canary()
    key = canary + "k" * (MAX_JSON_KEY_BYTES + 1 - len(canary.encode("utf-8")))
    assert len(key.encode("utf-8")) > MAX_JSON_KEY_BYTES
    payload = _cold_state_payload()
    _first_crack(payload)[key] = 0
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.JSON_KEY_INVALID
    assert raised.value.field_names == ()
    assert raised.value.args == (_TICK_MESSAGE,)
    _assert_contained(raised.value, canary)


class _SequencedCaller:
    """Transport returning successive per-tool responses from a queue."""

    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, tool: str, args: dict[str, object]) -> object:
        self.calls.append((tool, args))
        return self.responses[tool].pop(0)


@pytest.mark.asyncio
async def test_tick_state_and_audio_come_from_one_read() -> None:
    """T8: state and audio share one response even when a second would differ."""
    first = _cold_state_payload()
    second = _cold_state_payload()
    _first_crack(second)["mode"] = "audio"
    assert _first_crack(first)["mode"] != _first_crack(second)["mode"]
    caller = _SequencedCaller(
        {"start_roast_session": [_cold_start_payload()], "get_roast_state": [first, second]}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()

    observation = await client.get_roast_state()

    assert observation.state.first_crack_status.mode == observation.audio.audio.mode
    assert observation.audio.audio.mode == _first_crack(first)["mode"]
    assert [call[0] for call in caller.calls].count("get_roast_state") == 1


#: The six audio fields the strict sample declares as ``float`` or ``float | None``.
_FLOAT_AUDIO_FIELDS: tuple[str, ...] = (
    "detected_monotonic_seconds",
    "mic_peak_dbfs",
    "mic_rms_dbfs",
    "estimated_lost_audio_ms_last_minute",
    "last_inference_duration_ms",
    "max_inference_duration_ms",
)


def test_float_audio_field_inventory_matches_the_strict_sample() -> None:
    """The float-designated inventory is exactly the sample's float annotations."""
    declared = {
        name
        for name, info in ColdTickAudioSample.model_fields.items()
        if info.annotation in (float, float | None)
    }
    assert declared == set(_FLOAT_AUDIO_FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _FLOAT_AUDIO_FIELDS)
async def test_json_integer_in_float_audio_field_is_refused(field: str) -> None:
    """A raw JSON integer is never silently widened into float audio evidence."""
    payload = _cold_state_payload()
    _first_crack(payload)[field] = 1
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)

    with pytest.raises(ColdTickAudioProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT
    assert raised.value.field_names == (ColdAudioField(field),)
    assert raised.value.args == (_TICK_MESSAGE,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
async def test_json_floats_in_every_float_audio_field_are_accepted() -> None:
    """Correctly typed JSON floats keep their exact type and value."""
    payload = _cold_state_payload()
    for field in _FLOAT_AUDIO_FIELDS:
        _first_crack(payload)[field] = 1.5
    client, _ = await _started_client(payload)

    observation = await client.get_roast_state()

    for field in _FLOAT_AUDIO_FIELDS:
        value = getattr(observation.audio.audio, field)
        assert type(value) is float, field
        assert value == 1.5, field


class _RealProjections(typing.NamedTuple):
    """The strict non-temperature projections of one raw real-child tick."""

    state: RoastSessionState
    audio: ColdTickProjection
    device: ColdTickDeviceState | None
    roast_fan: ColdTickRoastFanObservation
    session: ColdTickSessionMetadata


async def _real_projections_without_temperature(
    process: MCPServerProcess, session_id: str
) -> _RealProjections:
    """Read one raw real-child tick and apply every strict projector but temperature.

    This keeps exact-type evidence for the existing projectors against the
    published MCP, whose responses the public read now refuses in full.
    """
    raw = await process.call_tool("get_roast_state", {"session_id": session_id})
    client_type = ColdCharacterisationMCPClient
    state, tree = client_type._parse_tick(raw)  # pyright: ignore[reportPrivateUsage]
    mapping = cast("dict[str, object]", tree)
    assert "cold_" + "temperature_projection" not in mapping
    raw_audio = cast("dict[str, ColdJsonValue]", mapping["first_crack_status"])
    audio = project_tick_audio(raw_audio)
    client_type._require_lossless_audio_types(raw_audio, audio)  # pyright: ignore[reportPrivateUsage]
    return _RealProjections(
        state=state,
        audio=audio,
        device=client_type._project_device(mapping["device_state"]),  # pyright: ignore[reportPrivateUsage]
        roast_fan=client_type._project_roast_fan(mapping),  # pyright: ignore[reportPrivateUsage]
        session=client_type._project_session(mapping),  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.serial(reason="drives a real MCP process group; must not run concurrently")
async def test_real_child_cold_tick_projects_strictly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    """T9/AC-P6, T-S8/AC-S6, #997 T17: the published mock MCP cold tick.

    Published 0.2.2 carries no temperature projection, so the public read fails
    closed with ``PROJECTION_KEY_MISSING`` and returns no observation.  The
    other strict projectors are then applied to raw responses read directly
    from the same real child, so their exact-type evidence against the
    published MCP is retained: the session metadata has exact types, and the
    MCP session clock strictly advances between two reads at least 1.0 s
    apart (the heartbeat premise).
    """
    monkeypatch.chdir(tmp_path)
    process = MCPServerProcess()
    await process.start()
    try:
        client = ColdCharacterisationMCPClient(process.call_tool)
        session_id = (await client.start_cold_session()).session.session_id
        await client.mark_beans_added()
        with pytest.raises(ColdTickTemperatureProjectionError) as raised:
            await client.get_roast_state()
        assert raised.value.failure is ColdTemperatureProjectionFailure.PROJECTION_KEY_MISSING
        observation = await _real_projections_without_temperature(process, session_id)
        assert observation.state.session_purpose == "cold_characterisation"
        assert type(observation.audio.audio) is ColdTickAudioSample
        audio = observation.audio.audio
        assert type(audio.max_consecutive_overflow_count) is int
        assert type(audio.inference_overrun_count) is int
        assert type(audio.last_inference_duration_ms) is float
        assert type(audio.max_inference_duration_ms) is float
        device = observation.device
        assert device is not None
        assert type(device) is ColdTickDeviceState
        assert type(device.driver) is str
        assert type(device.connected) is bool
        assert device.bean_temp_c is None or type(device.bean_temp_c) is float
        assert device.env_temp_c is None or type(device.env_temp_c) is float
        assert type(device.heat_level_percent) is int
        assert type(device.fan_level_percent) is int
        assert type(device.cooling_on) is bool
        assert type(device.raw_vendor_data) is dict
        roast_fan = observation.roast_fan
        assert type(roast_fan) is ColdTickRoastFanObservation
        assert type(roast_fan.outcome) is ColdRoastFanOutcome
        if roast_fan.outcome is ColdRoastFanOutcome.OBSERVED:
            assert type(roast_fan.roast_fan_level_percent) is int
            assert 0 <= roast_fan.roast_fan_level_percent <= 100
        else:
            assert roast_fan.roast_fan_level_percent is None
        first = observation.session
        await asyncio.sleep(1.1)
        second = (await _real_projections_without_temperature(process, session_id)).session
        for session in (first, second):
            assert type(session) is ColdTickSessionMetadata
            assert type(session.session_id) is str
            assert type(session.active) is bool
            assert session.active is True
            assert type(session.session_purpose) is str
            assert session.session_purpose == "cold_characterisation"
            assert type(session.phase) is str
            assert session.phase == "roasting"
            assert type(session.elapsed_monotonic_seconds) is float
        assert second.session_id == first.session_id
        record_property(
            "session_elapsed_pair",
            (first.elapsed_monotonic_seconds, second.elapsed_monotonic_seconds),
        )
        assert second.elapsed_monotonic_seconds > first.elapsed_monotonic_seconds
    finally:
        await process.stop()
    assert not process.running


def test_cold_tick_observation_declares_its_closed_configuration() -> None:
    """T11: configuration pin only; this is not a behavioural coercion proof."""
    config = dict(ColdTickObservation.model_config)
    assert config.get("strict") is True
    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"
    assert config.get("allow_inf_nan") is False


@pytest.mark.asyncio
async def test_cold_tick_observation_is_closed_and_frozen() -> None:
    """T11: the observation refuses unknown fields and reassignment."""
    client, _ = await _started_client(_cold_state_payload())
    observation = await client.get_roast_state()

    with pytest.raises(ValidationError):
        ColdTickObservation.model_validate(
            {
                "state": observation.state,
                "audio": observation.audio,
                "device": observation.device,
                "roast_fan": observation.roast_fan,
                "session": observation.session,
                "temperature": observation.temperature,
                "unexpected": 1,
            }
        )
    with pytest.raises(ValidationError):
        observation.audio = observation.audio
    with pytest.raises(ValidationError):
        observation.device = observation.device
    with pytest.raises(ValidationError):
        observation.session = observation.session


_TOLERANT_FIRST_CRACK_NAME = "first_crack_status"


def _cold_production_trees() -> dict[str, ast.Module]:
    """Parse every cold production module (docstrings and comments are not nodes)."""
    return {
        path.relative_to(_COLD_PACKAGE).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_COLD_PACKAGE.rglob("*.py"))
    }


def test_cold_production_code_never_reads_tolerant_first_crack_status() -> None:
    """T12: a finite syntax guard over literal, attribute, and keyword uses of the name.

    Across every cold production module it forbids ``.first_crack_status``
    attribute nodes, ``getattr`` with that literal, keyword arguments and
    class-pattern keywords of that name, and any exact ``"first_crack_status"``
    string constant except the single raw projection subscript in ``mcp.py``.
    Docstrings are longer strings, so they are not counted.  Access through a
    variable or a dynamically computed name is an explicit residual: this is a
    syntax guard, not a proof that the tolerant mirror is unreachable.
    """
    trees = _cold_production_trees()
    assert "mcp.py" in trees
    attributes: list[str] = []
    getattr_calls: list[str] = []
    keywords: list[str] = []
    pattern_keywords: list[str] = []
    constants: dict[str, list[ast.Constant]] = {}
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == _TOLERANT_FIRST_CRACK_NAME:
                attributes.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == _TOLERANT_FIRST_CRACK_NAME
                    for arg in node.args
                )
            ):
                getattr_calls.append(name)
            if isinstance(node, ast.keyword) and node.arg == _TOLERANT_FIRST_CRACK_NAME:
                keywords.append(name)
            if isinstance(node, ast.MatchClass) and _TOLERANT_FIRST_CRACK_NAME in node.kwd_attrs:
                pattern_keywords.append(name)
            if isinstance(node, ast.Constant) and node.value == _TOLERANT_FIRST_CRACK_NAME:
                constants.setdefault(name, []).append(node)
    assert attributes == []
    assert getattr_calls == []
    assert keywords == []
    assert pattern_keywords == []
    assert sorted(constants) == ["mcp.py"]
    assert len(constants["mcp.py"]) == 1
    subscripts = [
        node
        for node in ast.walk(trees["mcp.py"])
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == _TOLERANT_FIRST_CRACK_NAME
    ]
    assert len(subscripts) == 1
    assert subscripts[0].slice is constants["mcp.py"][0]


# --- #954 slice 4b: strict per-tick device-state projection -----------------

_DEVICE_MESSAGE = "MCP tick device state failed strict projection"
_TOLERANT_DEVICE_NAME = "device_state"


def _device(payload: dict[str, object]) -> dict[str, object]:
    """Return the mutable raw ``device_state`` mapping of a tick payload."""
    return cast("dict[str, object]", payload[_TOLERANT_DEVICE_NAME])


def _numeric_canary() -> str:
    """Assemble the numeric containment canary at runtime from fragments."""
    return "".join(["9081", "7263", "54"])


def _vendor_of_size(size: int) -> dict[str, object]:
    """Return a walker-admissible vendor map of exactly ``size`` canonical bytes."""
    vendor: dict[str, object] = {}
    index = 0
    while len(_canonical_json(vendor).encode("utf-8")) < size:
        vendor[f"k{index:02d}"] = "x" * 1_500
        index += 1
    key = f"k{index - 1:02d}"
    overshoot = len(_canonical_json(vendor).encode("utf-8")) - size
    vendor[key] = "x" * (1_500 - overshoot)
    assert len(_canonical_json(vendor).encode("utf-8")) == size
    return vendor


async def _device_error(payload: dict[str, object]) -> ColdTickDeviceProjectionError:
    """Read one tick through the public client and return its device projection error."""
    client, caller = await _started_client(payload)
    with pytest.raises(ColdTickDeviceProjectionError) as raised:
        await client.get_roast_state()
    assert len(_state_calls(caller)) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def _assert_device_contained(error: ColdTickDeviceProjectionError, canary: str) -> None:
    """Assert a canary is absent from every rendering and closed diagnostic of an error."""
    assert isinstance(error, ColdMcpValidationError)
    assert error.args == (_DEVICE_MESSAGE,)
    _assert_contained(error, canary)
    assert canary not in error.failure.value
    assert error.field is None or canary not in error.field.value


@pytest.mark.asyncio
async def test_cold_tick_device_equals_the_raw_values_exactly() -> None:
    """T-D1: every device field equals its raw value with an identical type, from one read."""
    payload = _cold_state_payload()
    raw = dict(_device(payload))
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    device = observation.device
    assert type(device) is ColdTickDeviceState
    for field in ColdDeviceField:
        value = getattr(device, field.value)
        assert value == raw[field.value], field
        assert type(value) is type(raw[field.value]), field
    assert device.heat_level_percent == 70
    assert _state_calls(caller) == [("get_roast_state", {"session_id": "session-id"})]
    direct = ColdCharacterisationMCPClient._project_device(raw)  # pyright: ignore[reportPrivateUsage]
    assert direct is not None
    assert direct.raw_vendor_data == raw["raw_vendor_data"]
    assert direct.raw_vendor_data is not raw["raw_vendor_data"]


@pytest.mark.asyncio
async def test_null_device_state_is_recorded_as_absent_not_as_a_device() -> None:
    """T-D2: JSON null is represented as ``None``, never as a zero device."""
    payload = _cold_state_payload()
    payload[_TOLERANT_DEVICE_NAME] = None
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    assert observation.device is None
    assert not isinstance(observation.device, ColdTickDeviceState)
    assert len(_state_calls(caller)) == 1


def _generic_device_cases() -> list[tuple[str, object]]:
    """Return mutations the tolerant mirror itself refuses before projection."""
    cases: list[tuple[str, object]] = [(field.value, "<delete>") for field in ColdDeviceField]
    cases.append(("driver", 1))
    cases.append(("raw_vendor_data", {"k": {"n": 1}}))
    return cases


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _generic_device_cases())
async def test_device_values_the_mirror_refuses_fail_generic_validation(
    field: str, value: object
) -> None:
    """T-D3a: a missing key or mirror-refused type fails with the generic error."""
    payload = _cold_state_payload()
    if value == "<delete>":
        del _device(payload)[field]
    else:
        _device(payload)[field] = value
    client, caller = await _started_client(payload)

    with pytest.raises(ColdMcpValidationError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdMcpValidationError
    assert str(raised.value) == _GENERIC_MESSAGE
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (ColdDeviceField.HEAT_LEVEL_PERCENT, "0"),
        (ColdDeviceField.HEAT_LEVEL_PERCENT, 0.0),
        (ColdDeviceField.HEAT_LEVEL_PERCENT, False),
        (ColdDeviceField.FAN_LEVEL_PERCENT, "0"),
        (ColdDeviceField.FAN_LEVEL_PERCENT, True),
        (ColdDeviceField.COOLING_ON, 0),
        (ColdDeviceField.COOLING_ON, "false"),
        (ColdDeviceField.CONNECTED, 1),
        (ColdDeviceField.CONNECTED, "true"),
        (ColdDeviceField.BEAN_TEMP_C, 20),
        (ColdDeviceField.BEAN_TEMP_C, "20.1"),
        (ColdDeviceField.ENV_TEMP_C, 21),
    ],
)
async def test_coerced_device_value_fails_strict_projection(
    field: ColdDeviceField, value: object
) -> None:
    """T-D3b: a value the tolerant mirror would coerce is refused, never coerced."""
    payload = _cold_state_payload()
    _device(payload)[field.value] = value
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT
    assert error.field is field
    assert str(error) == _DEVICE_MESSAGE


@pytest.mark.asyncio
async def test_unknown_device_key_is_refused_not_dropped() -> None:
    """T-D4: a key the tolerant mirror silently drops is refused by the projection."""
    payload = _cold_state_payload()
    _device(payload)["main_fan_level_percent"] = 0
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.FIELD_SET_MISMATCH
    assert error.field is None


@pytest.mark.asyncio
async def test_vendor_map_at_the_persistence_cap_projects() -> None:
    """T-D5: a vendor map of exactly the canonical cap is admitted unchanged."""
    payload = _cold_state_payload()
    vendor = _vendor_of_size(MAX_VENDOR_BLOB_BYTES)
    _device(payload)["raw_vendor_data"] = vendor
    _assert_tolerant_mirror_accepts(payload)
    client, _ = await _started_client(payload)

    observation = await client.get_roast_state()

    assert observation.device is not None
    assert observation.device.raw_vendor_data == vendor


@pytest.mark.asyncio
async def test_vendor_map_one_byte_over_the_cap_is_refused() -> None:
    """T-D5: one canonical byte over the persistence cap is refused at read time."""
    payload = _cold_state_payload()
    _device(payload)["raw_vendor_data"] = _vendor_of_size(MAX_VENDOR_BLOB_BYTES + 1)
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.VENDOR_DATA_TOO_LARGE
    assert error.field is ColdDeviceField.RAW_VENDOR_DATA


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["huge_int", "long_vendor_key"])
async def test_device_value_the_walker_refuses_is_not_admitted(case: str) -> None:
    """T-D5: walker bounds apply to the whole raw device state."""
    payload = _cold_state_payload()
    if case == "huge_int":
        _device(payload)["heat_level_percent"] = 10**40
    else:
        key = "v" * (MAX_JSON_KEY_BYTES + 1)
        _device(payload)["raw_vendor_data"] = {key: 1}
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.DEVICE_VALUE_NOT_ADMITTED
    assert error.field is None


@pytest.mark.asyncio
async def test_read_time_vendor_cap_matches_persistence() -> None:
    """T-D5 parity: an at-cap map persists, and the canonical helper is byte-identical."""
    client, _ = await _started_client(_cold_state_payload())
    observation = await client.get_roast_state()
    vendor = _vendor_of_size(MAX_VENDOR_BLOB_BYTES)
    device = observation.device
    assert device is not None
    record = ColdTickRecord.model_validate(
        {
            "schema_version": 1,
            "stream": "tick",
            "run_id": "20260904T143036Z-d183-char-fan-music-retry4",
            "phase": ColdPhaseKind.RECORDING_ON,
            "recorded_at_utc": "2026-09-04T14:30:36Z",
            "monotonic_seconds": 1.0,
            "identity_sha256": "a" * 64,
            "tick": 0,
            "device": ColdTickDeviceEvidence(
                driver=device.driver,
                connected=device.connected,
                bean_temp_c=device.bean_temp_c,
                env_temp_c=device.env_temp_c,
                heat_level_percent=device.heat_level_percent,
                fan_level_percent=device.fan_level_percent,
                cooling_on=device.cooling_on,
                raw_vendor_data=json.loads(_canonical_json(vendor)),
            ),
            "roast_fan": ColdTickRoastFanEvidence(
                outcome=ColdTickRoastFanOutcome(observation.roast_fan.outcome.value),
                roast_fan_level_percent=observation.roast_fan.roast_fan_level_percent,
            ),
            "session": ColdTickSessionEvidence(
                session_id=observation.session.session_id,
                active=observation.session.active,
                session_purpose=observation.session.session_purpose,
                phase=ColdTickSessionPhase(observation.session.phase),
                elapsed_monotonic_seconds=observation.session.elapsed_monotonic_seconds,
            ),
            "audio": observation.audio.audio,
        }
    )
    validated = validate_record(record)
    assert type(validated) is ColdTickRecord
    assert validated.device is not None
    assert validated.device.raw_vendor_data == vendor
    fixed: dict[str, object] = {"b": [1, 2.5, None], "a": "é", "c": {"z": True}}
    local = cold_mcp._canonical_vendor_json(fixed)  # pyright: ignore[reportPrivateUsage]
    assert local == _canonical_json(fixed)


def test_projector_refuses_non_object_and_names_the_first_missing_field() -> None:
    """T-D6: direct defensive paths report closed diagnostics."""
    project = ColdCharacterisationMCPClient._project_device  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ColdTickDeviceProjectionError) as raised:
        project([])
    assert raised.value.failure is ColdDeviceProjectionFailure.DEVICE_STATE_NOT_OBJECT
    assert raised.value.field is None

    raw = dict(_device(_cold_state_payload()))
    del raw["driver"]
    with pytest.raises(ColdTickDeviceProjectionError) as raised:
        project(raw)
    assert raised.value.failure is ColdDeviceProjectionFailure.FIELD_SET_MISMATCH
    assert raised.value.field is ColdDeviceField.DRIVER


@pytest.mark.asyncio
async def test_device_projection_failure_contains_a_rejected_value() -> None:
    """T-D7a/AC-D6: a coerced numeric string never reaches the error chain."""
    canary = _numeric_canary()
    payload = _cold_state_payload()
    _device(payload)["heat_level_percent"] = canary
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT
    assert error.field is ColdDeviceField.HEAT_LEVEL_PERCENT
    _assert_device_contained(error, canary)


@pytest.mark.asyncio
async def test_device_projection_failure_contains_a_rejected_key_name() -> None:
    """T-D7b/AC-D6: an unknown key name never reaches the error chain."""
    canary = _numeric_canary()
    key = "rpcanary" + canary
    assert len(key.encode("utf-8")) < MAX_JSON_KEY_BYTES
    payload = _cold_state_payload()
    _device(payload)[key] = 0
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.FIELD_SET_MISMATCH
    assert error.field is None
    _assert_device_contained(error, canary)


@pytest.mark.asyncio
async def test_audio_projection_precedes_device_projection() -> None:
    """T-D8: incomplete audio evidence wins over a bad device state."""
    payload = _cold_state_payload()
    del _first_crack(payload)["max_consecutive_overflow_count"]
    _device(payload)["heat_level_percent"] = "0"
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is ColdTickAudioProjectionError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("session_id", "other-session", ColdSessionIdentityError),
        ("session_purpose", "roast", ColdSessionPurposeError),
    ],
)
async def test_identity_and_purpose_checks_precede_device_projection(
    field: str, value: str, error: type[ColdMcpError]
) -> None:
    """T-D8: a wrong session or purpose wins over a bad device state."""
    payload = _cold_state_payload()
    payload[field] = value
    _device(payload)["heat_level_percent"] = "0"
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is error


@pytest.mark.asyncio
async def test_tick_state_and_device_come_from_one_read() -> None:
    """T-D8b: state and device share one response even when a second would differ."""
    first = _cold_state_payload()
    second = _cold_state_payload()
    _device(second)["driver"] = "other-driver"
    caller = _SequencedCaller(
        {"start_roast_session": [_cold_start_payload()], "get_roast_state": [first, second]}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()

    observation = await client.get_roast_state()

    assert observation.device is not None
    assert observation.state.device_state is not None
    assert observation.device.driver == observation.state.device_state.driver
    assert observation.device.driver == _device(first)["driver"]
    assert [call[0] for call in caller.calls].count("get_roast_state") == 1


def test_cold_tick_device_state_declares_its_closed_configuration() -> None:
    """T-D11: configuration pin only; behaviour is proved by the projection tests."""
    config = dict(ColdTickDeviceState.model_config)
    assert config.get("strict") is True
    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"
    assert config.get("allow_inf_nan") is False


def test_cold_tick_device_state_refuses_unknown_fields() -> None:
    """T-D11: eight valid fields plus an unknown key are refused."""
    raw = dict(_device(_cold_state_payload()))
    ColdTickDeviceState.model_validate(raw)
    with pytest.raises(ValidationError):
        ColdTickDeviceState.model_validate({**raw, "x": 1})


def test_cold_production_code_never_reads_tolerant_device_state() -> None:
    """T-D12: a finite syntax guard over literal, attribute, and keyword uses of the name.

    Across every cold production module it forbids ``.device_state`` attribute
    nodes, ``getattr`` with that literal, keyword arguments and class-pattern
    keywords of that name, and any exact ``"device_state"`` string constant
    except the single raw projection subscript in ``mcp.py``.  Parameter and
    plain-name uses (the builder's ``device_state`` argument) are not counted.
    Access through a variable or a dynamically computed name is an explicit
    residual: this is a syntax guard, not a proof that the tolerant mirror is
    unreachable.
    """
    trees = _cold_production_trees()
    assert "mcp.py" in trees
    attributes: list[str] = []
    getattr_calls: list[str] = []
    keywords: list[str] = []
    pattern_keywords: list[str] = []
    constants: dict[str, list[ast.Constant]] = {}
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == _TOLERANT_DEVICE_NAME:
                attributes.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == _TOLERANT_DEVICE_NAME
                    for arg in node.args
                )
            ):
                getattr_calls.append(name)
            if isinstance(node, ast.keyword) and node.arg == _TOLERANT_DEVICE_NAME:
                keywords.append(name)
            if isinstance(node, ast.MatchClass) and _TOLERANT_DEVICE_NAME in node.kwd_attrs:
                pattern_keywords.append(name)
            if isinstance(node, ast.Constant) and node.value == _TOLERANT_DEVICE_NAME:
                constants.setdefault(name, []).append(node)
    assert attributes == []
    assert getattr_calls == []
    assert keywords == []
    assert pattern_keywords == []
    assert sorted(constants) == ["mcp.py"]
    assert len(constants["mcp.py"]) == 1
    subscripts = [
        node
        for node in ast.walk(trees["mcp.py"])
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == _TOLERANT_DEVICE_NAME
    ]
    assert len(subscripts) == 1
    assert subscripts[0].slice is constants["mcp.py"][0]


def test_device_enums_are_closed_plain_enums_matching_the_mirror() -> None:
    """T-D13: the eight device names match the 0.2.1 mirror in order; enums are plain."""
    assert [member.value for member in ColdDeviceField] == list(RoasterDeviceState.model_fields)
    assert len(ColdDeviceField) == 8
    for enum_type in (ColdDeviceField, ColdDeviceProjectionFailure):
        assert enum_type.__mro__[1:] == (Enum, object)
    assert {member.name for member in ColdDeviceProjectionFailure} == {
        "DEVICE_STATE_NOT_OBJECT",
        "FIELD_SET_MISMATCH",
        "FIELD_TYPE_NOT_EXACT",
        "DEVICE_VALUE_NOT_ADMITTED",
        "VENDOR_DATA_TOO_LARGE",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nulled",
    [
        (ColdDeviceField.BEAN_TEMP_C,),
        (ColdDeviceField.ENV_TEMP_C,),
        (ColdDeviceField.BEAN_TEMP_C, ColdDeviceField.ENV_TEMP_C),
    ],
)
async def test_null_temperatures_project_as_none_exactly(
    nulled: tuple[ColdDeviceField, ...],
) -> None:
    """T-D2b: a JSON-null temperature is ``None`` in a present device, never ``0.0``."""
    payload = _cold_state_payload()
    for field in nulled:
        _device(payload)[field.value] = None
    _assert_tolerant_mirror_accepts(payload)
    raw = dict(_device(payload))
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    device = observation.device
    assert type(device) is ColdTickDeviceState
    for field in ColdDeviceField:
        value = getattr(device, field.value)
        if field in nulled:
            assert value is None, field
            assert value != 0.0, field
        else:
            assert value == raw[field.value], field
            assert type(value) is type(raw[field.value]), field
    assert len(_state_calls(caller)) == 1


def test_projector_maps_both_null_temperatures_to_none_directly() -> None:
    """T-D2b: the direct projector gives the same result for both temperatures null."""
    raw = dict(_device(_cold_state_payload()))
    raw["bean_temp_c"] = None
    raw["env_temp_c"] = None

    device = ColdCharacterisationMCPClient._project_device(raw)  # pyright: ignore[reportPrivateUsage]

    assert type(device) is ColdTickDeviceState
    assert device.bean_temp_c is None
    assert device.env_temp_c is None
    assert device.driver == raw["driver"]
    assert device.heat_level_percent == raw["heat_level_percent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_device_state_is_recorded_as_data_without_a_verdict(connected: bool) -> None:
    """T-D16/AC-D5: the projection records these raw values exactly and returns no verdict."""
    recorded: dict[ColdDeviceField, object] = {
        ColdDeviceField.CONNECTED: connected,
        ColdDeviceField.COOLING_ON: True,
        ColdDeviceField.HEAT_LEVEL_PERCENT: 100,
        ColdDeviceField.FAN_LEVEL_PERCENT: 55,
    }
    payload = _cold_state_payload()
    for field, value in recorded.items():
        _device(payload)[field.value] = value
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    device = observation.device
    assert type(device) is ColdTickDeviceState
    for field, value in recorded.items():
        projected = getattr(device, field.value)
        assert projected == value, field
        assert type(projected) is type(value), field
    assert set(ColdTickDeviceState.model_fields) == {field.value for field in ColdDeviceField}
    assert not isinstance(device, SafetyEvaluation | SafetyVerdict)
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
async def test_step_six_guard_refuses_a_scalar_whose_exact_type_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D17: with step 4 broadened, the post-construct exact-type guard still refuses."""
    broadened: tuple[type, ...] = (float, int, type(None))
    monkeypatch.setitem(
        cold_mcp._DEVICE_FIELD_TYPES,  # pyright: ignore[reportPrivateUsage]
        ColdDeviceField.BEAN_TEMP_C,
        broadened,
    )
    assert cold_mcp._has_exact_type(20, broadened)  # pyright: ignore[reportPrivateUsage]
    payload = _cold_state_payload()
    _device(payload)["bean_temp_c"] = 20
    _assert_tolerant_mirror_accepts(payload)

    error = await _device_error(payload)

    assert error.failure is ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT
    assert error.field is ColdDeviceField.BEAN_TEMP_C
    assert str(error) == _DEVICE_MESSAGE


# --- #954 slice 4c: strict commanded roast-fan observation ------------------

_ROAST_FAN_MESSAGE = "MCP tick roast-fan observation failed strict projection"
_ROAST_FAN_FIELD = "cold_characterisation_observation"


def _roast_fan(payload: dict[str, object]) -> dict[str, object]:
    """Return the mutable raw commanded-roast-fan mapping from a tick payload."""
    return cast("dict[str, object]", payload[_ROAST_FAN_FIELD])


async def _roast_fan_error(
    payload: dict[str, object],
) -> ColdTickRoastFanProjectionError:
    """Read one tick through the public client and return its fan projection error."""
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)
    with pytest.raises(ColdTickRoastFanProjectionError) as raised:
        await client.get_roast_state()
    assert len(_state_calls(caller)) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "level"),
    [
        (ColdRoastFanOutcome.OBSERVED, 0),
        (ColdRoastFanOutcome.OBSERVED, 100),
        (ColdRoastFanOutcome.NOT_ELIGIBLE, None),
        (ColdRoastFanOutcome.UNSUPPORTED, None),
        (ColdRoastFanOutcome.UNREADABLE, None),
        (ColdRoastFanOutcome.MALFORMED, None),
    ],
)
async def test_cold_tick_projects_closed_roast_fan_outcomes(
    outcome: ColdRoastFanOutcome, level: int | None
) -> None:
    """T-C1: every admitted outcome projects the exact commanded value from one read."""
    payload = _cold_state_payload()
    _roast_fan(payload).update({"outcome": outcome.value, "roast_fan_level_percent": level})
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    assert type(observation.roast_fan) is ColdTickRoastFanObservation
    assert observation.roast_fan.outcome is outcome
    assert observation.roast_fan.roast_fan_level_percent == level
    if outcome is ColdRoastFanOutcome.OBSERVED:
        assert type(observation.roast_fan.roast_fan_level_percent) is int
    else:
        assert observation.roast_fan.roast_fan_level_percent is None
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        ("missing", ColdRoastFanProjectionFailure.OBSERVATION_KEY_MISSING),
        ("null", ColdRoastFanProjectionFailure.OBSERVATION_NULL),
        ("not_object", ColdRoastFanProjectionFailure.OBSERVATION_NOT_OBJECT),
        ("extra_key", ColdRoastFanProjectionFailure.FIELD_SET_MISMATCH),
        ("missing_outcome", ColdRoastFanProjectionFailure.FIELD_SET_MISMATCH),
        ("null_outcome", ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED),
        ("number_outcome", ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED),
        ("bad_outcome", ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED),
        ("bad_outcome_case", ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED),
        ("bad_outcome_space", ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED),
        ("bool_level", ColdRoastFanProjectionFailure.LEVEL_TYPE_NOT_EXACT),
        ("float_level", ColdRoastFanProjectionFailure.LEVEL_TYPE_NOT_EXACT),
        ("string_level", ColdRoastFanProjectionFailure.LEVEL_TYPE_NOT_EXACT),
        ("low_level", ColdRoastFanProjectionFailure.LEVEL_OUT_OF_RANGE),
        ("high_level", ColdRoastFanProjectionFailure.LEVEL_OUT_OF_RANGE),
        ("observed_null", ColdRoastFanProjectionFailure.LEVEL_OUTCOME_MISMATCH),
        ("non_observed_level", ColdRoastFanProjectionFailure.LEVEL_OUTCOME_MISMATCH),
    ],
)
async def test_cold_tick_roast_fan_projection_fails_closed(
    mutation: str, failure: ColdRoastFanProjectionFailure
) -> None:
    """T-C2: every malformed raw roast-fan shape gets one closed failure."""
    payload = _cold_state_payload()
    raw = _roast_fan(payload)
    if mutation == "missing":
        del payload[_ROAST_FAN_FIELD]
    elif mutation == "null":
        payload[_ROAST_FAN_FIELD] = None
    elif mutation == "not_object":
        payload[_ROAST_FAN_FIELD] = []
    elif mutation == "extra_key":
        raw["main_fan_level_percent"] = 0
    elif mutation == "missing_outcome":
        del raw["outcome"]
    elif mutation == "null_outcome":
        raw["outcome"] = None
    elif mutation == "number_outcome":
        raw["outcome"] = 0
    elif mutation == "bad_outcome":
        raw["outcome"] = "stalled"
    elif mutation == "bad_outcome_case":
        raw["outcome"] = "Observed"
    elif mutation == "bad_outcome_space":
        raw["outcome"] = " observed"
    elif mutation == "bool_level":
        raw["roast_fan_level_percent"] = False
    elif mutation == "float_level":
        raw["roast_fan_level_percent"] = 0.0
    elif mutation == "string_level":
        raw["roast_fan_level_percent"] = "0"
    elif mutation == "low_level":
        raw["roast_fan_level_percent"] = -1
    elif mutation == "high_level":
        raw["roast_fan_level_percent"] = 101
    elif mutation == "observed_null":
        raw["roast_fan_level_percent"] = None
    else:
        raw["outcome"] = "not_eligible"
        raw["roast_fan_level_percent"] = 0

    error = await _roast_fan_error(payload)

    assert error.failure is failure
    assert error.args == (_ROAST_FAN_MESSAGE,)
    assert str(error) == _ROAST_FAN_MESSAGE


@pytest.mark.asyncio
async def test_roast_fan_projection_contains_untrusted_value_and_key() -> None:
    """T-C3: malformed values and keys are never included in public diagnostics."""
    canary = _numeric_canary()
    payload = _cold_state_payload()
    _roast_fan(payload)["roast_fan_level_percent"] = canary
    error = await _roast_fan_error(payload)
    assert error.failure is ColdRoastFanProjectionFailure.LEVEL_TYPE_NOT_EXACT
    _assert_contained(error, canary)

    payload = _cold_state_payload()
    key = "unexpected_" + canary
    assert len(key.encode("utf-8")) < MAX_JSON_KEY_BYTES
    _roast_fan(payload)[key] = 0
    error = await _roast_fan_error(payload)
    assert error.failure is ColdRoastFanProjectionFailure.FIELD_SET_MISMATCH
    _assert_contained(error, canary)

    payload = _cold_state_payload()
    _roast_fan(payload)["outcome"] = canary
    error = await _roast_fan_error(payload)
    assert error.failure is ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED
    _assert_contained(error, canary)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        ("identity", ColdSessionIdentityError),
        ("purpose", ColdSessionPurposeError),
        ("audio", ColdTickAudioProjectionError),
        ("device", ColdTickDeviceProjectionError),
    ],
)
async def test_earlier_cold_tick_projections_precede_roast_fan(
    earlier: str, expected: type[ColdMcpError]
) -> None:
    """T-C4: identity, purpose, audio, and device failures win over fan failure."""
    payload = _cold_state_payload()
    _roast_fan(payload)["roast_fan_level_percent"] = "0"
    if earlier == "identity":
        payload["session_id"] = "other-session"
    elif earlier == "purpose":
        payload["session_purpose"] = "roast"
    elif earlier == "audio":
        del _first_crack(payload)["max_consecutive_overflow_count"]
    else:
        _device(payload)["heat_level_percent"] = "0"
    client, _ = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is expected


@pytest.mark.asyncio
async def test_tick_state_and_roast_fan_come_from_one_read() -> None:
    """T-C5: the roast fan shares one response with state even if another differs."""
    first = _cold_state_payload()
    second = _cold_state_payload()
    _roast_fan(second)["roast_fan_level_percent"] = 100
    caller = _SequencedCaller(
        {"start_roast_session": [_cold_start_payload()], "get_roast_state": [first, second]}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()

    observation = await client.get_roast_state()

    assert observation.roast_fan.roast_fan_level_percent == 0
    assert [call[0] for call in caller.calls].count("get_roast_state") == 1


def test_cold_tick_roast_fan_model_is_closed_and_records_no_verdict() -> None:
    """T-C6/C7: the strict observation is closed, frozen, and policy-free."""
    config = dict(ColdTickRoastFanObservation.model_config)
    assert config == {
        **config,
        "strict": True,
        "frozen": True,
        "extra": "forbid",
        "allow_inf_nan": False,
    }
    assert set(ColdTickRoastFanObservation.model_fields) == {
        "outcome",
        "roast_fan_level_percent",
    }
    for outcome in ColdRoastFanOutcome:
        level = 100 if outcome is ColdRoastFanOutcome.OBSERVED else None
        observation = ColdTickRoastFanObservation.model_validate(
            {"outcome": outcome, "roast_fan_level_percent": level}, strict=True
        )
        assert not isinstance(observation, SafetyEvaluation | SafetyVerdict)
    with pytest.raises(ValidationError):
        ColdTickRoastFanObservation.model_validate(
            {"outcome": ColdRoastFanOutcome.OBSERVED, "roast_fan_level_percent": None},
            strict=True,
        )
    with pytest.raises(ValidationError):
        ColdTickRoastFanObservation.model_validate(
            {"outcome": ColdRoastFanOutcome.NOT_ELIGIBLE, "roast_fan_level_percent": 0},
            strict=True,
        )
    with pytest.raises(ValidationError):
        ColdTickRoastFanObservation.model_validate(
            {
                "outcome": ColdRoastFanOutcome.OBSERVED,
                "roast_fan_level_percent": 0,
                "unexpected": 1,
            },
            strict=True,
        )


def test_cold_roast_fan_enums_are_closed_plain_enums() -> None:
    """T-C6: outcome and failure enums are exact plain enums, never strings."""
    assert [outcome.value for outcome in ColdRoastFanOutcome] == [
        "observed",
        "not_eligible",
        "unsupported",
        "unreadable",
        "malformed",
    ]
    for enum_type in (ColdRoastFanOutcome, ColdRoastFanProjectionFailure):
        assert enum_type.__mro__[1:] == (Enum, object)
    assert {failure.name for failure in ColdRoastFanProjectionFailure} == {
        "OBSERVATION_KEY_MISSING",
        "OBSERVATION_NULL",
        "OBSERVATION_NOT_OBJECT",
        "FIELD_SET_MISMATCH",
        "OUTCOME_NOT_ADMITTED",
        "LEVEL_TYPE_NOT_EXACT",
        "LEVEL_OUT_OF_RANGE",
        "LEVEL_OUTCOME_MISMATCH",
    }


def test_cold_production_code_never_reads_tolerant_roast_fan_observation() -> None:
    """T-C8: one raw subscript is the only production access to the new key."""
    target = "cold_" + "characterisation_observation"
    attributes: list[str] = []
    getattr_calls: list[str] = []
    constants: dict[str, list[ast.Constant]] = {}
    trees = _cold_production_trees()
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == target:
                attributes.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(isinstance(arg, ast.Constant) and arg.value == target for arg in node.args)
            ):
                getattr_calls.append(name)
            if isinstance(node, ast.Constant) and node.value == target:
                constants.setdefault(name, []).append(node)
    assert attributes == []
    assert getattr_calls == []
    assert sorted(constants) == ["mcp.py"]
    assert len(constants["mcp.py"]) == 1
    subscripts = [
        node
        for node in ast.walk(trees["mcp.py"])
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == target
    ]
    assert len(subscripts) == 1
    assert subscripts[0].slice is constants["mcp.py"][0]


# --- #954 slice 4f-a: strict per-tick session metadata -----------------------

_SESSION_MESSAGE = "MCP tick session metadata failed strict projection"
_TOLERANT_SESSION_NAMES = frozenset({"active", "elapsed_monotonic_seconds"})


async def _session_error(payload: dict[str, object]) -> ColdTickSessionProjectionError:
    """Read one tolerant-admitted tick and return its session projection error."""
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)
    with pytest.raises(ColdTickSessionProjectionError) as raised:
        await client.get_roast_state()
    assert len(_state_calls(caller)) == 1
    assert isinstance(raised.value, ColdMcpValidationError)
    assert raised.value.args == (_SESSION_MESSAGE,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


@pytest.mark.asyncio
async def test_cold_tick_session_equals_the_raw_values_exactly() -> None:
    """T-S1/AC-S1: every session field equals its raw value with an identical type."""
    payload = _cold_state_payload()
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    session = observation.session
    assert type(session) is ColdTickSessionMetadata
    for field in ColdSessionField:
        value: object = getattr(session, field.value)
        assert value == payload[field.value], field
        assert type(value) is type(payload[field.value]), field
    assert session.active is True
    assert session.elapsed_monotonic_seconds == 0.059
    assert session.phase == "roasting"
    assert session.session_purpose == "cold_characterisation"
    assert _state_calls(caller) == [("get_roast_state", {"session_id": "session-id"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (ColdSessionField.ACTIVE, "true"),
        (ColdSessionField.ACTIVE, 1),
        (ColdSessionField.ACTIVE, 0),
        (ColdSessionField.ELAPSED_MONOTONIC_SECONDS, 1),
        (ColdSessionField.ELAPSED_MONOTONIC_SECONDS, "0.5"),
    ],
)
async def test_coerced_session_value_fails_strict_projection(
    field: ColdSessionField, value: object
) -> None:
    """T-S2/AC-S2: a value the tolerant mirror coerces is refused with its closed field."""
    payload = _cold_state_payload()
    payload[field.value] = value

    error = await _session_error(payload)

    assert error.field is field


@pytest.mark.asyncio
async def test_session_projection_failure_contains_the_rejected_value() -> None:
    """T-S3/AC-S5: a rejected clock value never reaches any rendering of the error."""
    canary = "".join(["9081", "7263", ".54"])
    payload = _cold_state_payload()
    payload[ColdSessionField.ELAPSED_MONOTONIC_SECONDS.value] = canary

    error = await _session_error(payload)

    assert error.field is ColdSessionField.ELAPSED_MONOTONIC_SECONDS
    _assert_contained(error, canary)
    assert canary not in error.field.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        ("identity", ColdSessionIdentityError),
        ("purpose", ColdSessionPurposeError),
        ("audio", ColdTickAudioProjectionError),
        ("device", ColdTickDeviceProjectionError),
        ("roast_fan", ColdTickRoastFanProjectionError),
    ],
)
async def test_earlier_cold_tick_checks_precede_session_projection(
    earlier: str, expected: type[ColdMcpError]
) -> None:
    """T-S4/AC-S4: identity, purpose, audio, device, and roast fan win over session."""
    payload = _cold_state_payload()
    payload[ColdSessionField.ACTIVE.value] = "true"
    if earlier == "identity":
        payload["session_id"] = "other-session"
    elif earlier == "purpose":
        payload["session_purpose"] = "roast"
    elif earlier == "audio":
        del _first_crack(payload)["max_consecutive_overflow_count"]
    elif earlier == "device":
        _device(payload)["heat_level_percent"] = "0"
    else:
        _roast_fan(payload)["roast_fan_level_percent"] = "0"
    client, caller = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is expected
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
async def test_tick_state_and_session_come_from_one_read() -> None:
    """T-S5/AC-S1: session metadata shares one response with state."""
    first = _cold_state_payload()
    second = _cold_state_payload()
    second[ColdSessionField.ACTIVE.value] = False
    second[ColdSessionField.PHASE.value] = "fault"
    second[ColdSessionField.ELAPSED_MONOTONIC_SECONDS.value] = 99.5
    caller = _SequencedCaller(
        {"start_roast_session": [_cold_start_payload()], "get_roast_state": [first, second]}
    )
    client = ColdCharacterisationMCPClient(caller)
    await client.start_cold_session()

    observation = await client.get_roast_state()

    assert observation.session.session_id == observation.state.session_id
    assert observation.session.active is True
    assert observation.session.phase == "roasting"
    assert observation.session.elapsed_monotonic_seconds == 0.059
    assert [call[0] for call in caller.calls].count("get_roast_state") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (ColdSessionField.ACTIVE, False),
        (ColdSessionField.PHASE, "fault"),
        (ColdSessionField.ELAPSED_MONOTONIC_SECONDS, -1.0),
        (ColdSessionField.ELAPSED_MONOTONIC_SECONDS, 0.0),
    ],
)
async def test_session_metadata_is_recorded_as_data_without_a_verdict(
    field: ColdSessionField, value: object
) -> None:
    """T-S6/AC-S3: inactive, faulted, or odd clock values project unchanged."""
    payload = _cold_state_payload()
    payload[field.value] = value
    client, _ = await _started_client(payload)

    observation = await client.get_roast_state()

    projected: object = getattr(observation.session, field.value)
    assert projected == value
    assert type(projected) is type(value)
    assert not isinstance(observation.session, SafetyEvaluation | SafetyVerdict)


def test_cold_tick_session_metadata_is_closed_frozen_and_required() -> None:
    """T-S7: the strict session model is closed, frozen, and required on the tick."""
    config = dict(ColdTickSessionMetadata.model_config)
    assert config == {
        **config,
        "strict": True,
        "frozen": True,
        "extra": "forbid",
        "allow_inf_nan": False,
    }
    assert list(ColdTickSessionMetadata.model_fields) == [field.value for field in ColdSessionField]
    assert all(info.is_required() for info in ColdTickSessionMetadata.model_fields.values())
    values: dict[str, object] = {
        "session_id": "session-id",
        "active": True,
        "session_purpose": "cold_characterisation",
        "phase": "roasting",
        "elapsed_monotonic_seconds": 0.5,
    }
    session = ColdTickSessionMetadata.model_validate(values, strict=True)
    with pytest.raises(ValidationError):
        ColdTickSessionMetadata.model_validate({**values, "unexpected": 1}, strict=True)
    with pytest.raises(ValidationError):
        session.active = False
    assert ColdTickObservation.model_fields["session"].is_required()
    assert set(ColdTickObservation.model_fields) == {
        "state",
        "audio",
        "device",
        "roast_fan",
        "session",
        "temperature",
    }


def test_session_enum_is_a_closed_plain_enum_matching_the_raw_keys() -> None:
    """T-S7: the closed session field enum is a plain enum over the five raw keys."""
    assert ColdSessionField.__mro__[1:] == (Enum, object)
    assert [field.value for field in ColdSessionField] == [
        "session_id",
        "active",
        "session_purpose",
        "phase",
        "elapsed_monotonic_seconds",
    ]
    assert {field.value for field in ColdSessionField} <= set(RoastSessionState.model_fields)


def test_session_projector_refuses_exactly_typed_values_the_model_refuses() -> None:
    """T-S7: exactly typed but model-refused values fail closed without a field."""
    tree: dict[str, object] = {
        "session_id": "session-id",
        "active": True,
        "session_purpose": "roast",
        "phase": "roasting",
        "elapsed_monotonic_seconds": 0.5,
    }
    with pytest.raises(ColdTickSessionProjectionError) as raised:
        ColdCharacterisationMCPClient._project_session(tree)  # pyright: ignore[reportPrivateUsage]

    assert raised.value.field is None
    assert raised.value.args == (_SESSION_MESSAGE,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_cold_production_code_never_reads_tolerant_session_metadata() -> None:
    """T-S9: no cold production getattr or subscript access to the tolerant names.

    Attribute reads of the names are governed by ``_session_read_violations``:
    only the closed ``_SESSION_READ_SITES`` table's single receiver binding per
    module (the tick builder's ``source_session`` and the engine policy's
    ``retained_session``) may read them.  This is a syntax guard over attribute
    nodes and literal ``getattr``
    calls, not a proof that the tolerant mirror is unreachable.  The strict
    projector reads the raw keys through ``ColdSessionField`` values, so no
    subscript uses these names as a literal constant.
    """
    trees = _cold_production_trees()
    assert "mcp.py" in trees
    attributes: dict[str, list[str]] = {}
    getattr_calls: list[str] = []
    subscripts: dict[str, int] = dict.fromkeys(sorted(_TOLERANT_SESSION_NAMES), 0)
    for name, tree in trees.items():
        attributes[name] = _session_read_violations(name, tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(
                    isinstance(arg, ast.Constant) and arg.value in _TOLERANT_SESSION_NAMES
                    for arg in node.args
                )
            ):
                getattr_calls.append(name)
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in _TOLERANT_SESSION_NAMES
            ):
                subscripts[node.slice.value] += 1
    assert "evidence_builders.py" in attributes
    assert "engine_policy.py" in attributes
    assert "engine.py" in attributes
    assert all(violations == [] for violations in attributes.values()), attributes
    assert getattr_calls == []
    assert subscripts == {"active": 0, "elapsed_monotonic_seconds": 0}


class _SessionReadSite(typing.NamedTuple):
    function: str
    receiver: str
    parameter: str


_SESSION_READ_SITES: typing.Final = types.MappingProxyType(
    {
        "evidence_builders.py": _SessionReadSite(
            "build_tick_record", "source_session", "observation"
        ),
        "engine_policy.py": _SessionReadSite("evaluate_tick", "retained_session", "record"),
    }
)


def _bindings_of(root: ast.AST, name: str) -> list[tuple[str, ast.AST]]:
    """Return every enumerated binding form of ``name`` under ``root`` (B1-B9)."""
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(root):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            found.append(("B1", node))
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Del):
            found.append(("B2", node))
        if isinstance(node, ast.arg) and node.arg == name:
            found.append(("B3", node))
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            found.append(("B4", node))
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(("B5", node))
        if isinstance(node, ast.MatchAs) and node.name == name:
            found.append(("B6", node))
        if isinstance(node, ast.MatchStar) and node.name == name:
            found.append(("B7", node))
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            found.append(("B8", node))
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            found.append(("B9", node))
    return found


def _is_parameter_session(value: ast.expr, parameter: str) -> bool:
    """Whether an expression is exactly ``<parameter>.session``."""
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "session"
        and isinstance(value.value, ast.Name)
        and value.value.id == parameter
        and isinstance(value.value.ctx, ast.Load)
    )


def _session_read_violations(module_name: str, tree: ast.Module) -> list[str]:
    """Return every governed session-attribute read or binding breaching R1-R6.

    This is a finite syntax guard over the enumerated binding and read forms;
    not a whole-program reachability proof.
    """
    governed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in _TOLERANT_SESSION_NAMES
    ]
    violations: list[str] = []
    # R1: only a module keyed in the closed site table may read the governed names.
    site = _SESSION_READ_SITES.get(module_name)
    if site is None:
        return [f"R1:{ast.unparse(node)}" for node in governed]
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == site.function
    ]
    if len(functions) != 1:
        return [f"R2:{site.function}:{len(functions)}"]
    (function,) = functions
    inside = {id(node) for node in ast.walk(function)}
    # R2: every governed read lies lexically inside the site's function.
    for node in governed:
        if id(node) not in inside:
            violations.append(f"R2:{ast.unparse(node)}")
    # R3: every governed read's receiver is exactly the site's loaded receiver.
    for node in governed:
        receiver = node.value
        if not (
            isinstance(receiver, ast.Name)
            and receiver.id == site.receiver
            and isinstance(receiver.ctx, ast.Load)
        ):
            violations.append(f"R3:{ast.unparse(node)}")
    # R4: exactly one governed read of each name across the module.
    for attribute in sorted(_TOLERANT_SESSION_NAMES):
        count = sum(1 for node in governed if node.attr == attribute)
        if count != 1:
            violations.append(f"R4:{attribute}:{count}")
    # R5a: exactly one authorised ``<receiver> = <parameter>.session``.
    authorised: list[ast.Assign] = []
    for statement in function.body:
        if not isinstance(statement, ast.Assign):
            continue
        if len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not (
            isinstance(target, ast.Name)
            and target.id == site.receiver
            and isinstance(target.ctx, ast.Store)
        ):
            continue
        if not _is_parameter_session(statement.value, site.parameter):
            continue
        authorised.append(statement)
    if len(authorised) != 1:
        violations.append(f"R5a:{len(authorised)}")
    authorised_targets = {id(statement.targets[0]) for statement in authorised}
    # R5b: no other binding of the receiver anywhere in the module.
    for category, node in _bindings_of(tree, site.receiver):
        if id(node) not in authorised_targets:
            violations.append(f"R5b:{category}")
    # R6: the site's parameter is present and is never rebound inside the function.
    arguments = function.args
    parameters = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *([arguments.vararg] if arguments.vararg is not None else []),
        *([arguments.kwarg] if arguments.kwarg is not None else []),
    ]
    own = [parameter for parameter in parameters if parameter.arg == site.parameter]
    if len(own) != 1:
        violations.append("R6:parameter")
    own_ids = {id(parameter) for parameter in own}
    for category, node in _bindings_of(function, site.parameter):
        if id(node) not in own_ids:
            violations.append(f"R6:{category}")
    return violations


_VALID_SESSION_SPECIMEN = """
def build_tick_record(*, observation):
    source_session = observation.session
    a = source_session.active
    b = source_session.elapsed_monotonic_seconds
"""
_B_LINE = "    b = source_session.elapsed_monotonic_seconds\n"
_ASSIGN_LINE = "    source_session = observation.session\n"


def _appended(block: str) -> str:
    """Return the valid specimen with one indented block appended to the function."""
    return _VALID_SESSION_SPECIMEN + "".join(f"    {line}\n" for line in block.splitlines())


#: One representative probe per rule or binding branch (§2.6(b)); each changes one property.
_SESSION_PROBES: dict[str, tuple[str, str]] = {
    "P1-R3-receiver": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN.replace(
            "a = source_session.active", "a = observation.session.active"
        ),
    ),
    "P3-R5a-value": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN.replace("= observation.session\n", "= observation.state\n"),
    ),
    "P4-R5a-count": ("evidence_builders.py", _VALID_SESSION_SPECIMEN + _ASSIGN_LINE),
    "P19-R5a-direct-child": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN.replace(_ASSIGN_LINE, "    if flag:\n    " + _ASSIGN_LINE),
    ),
    "P5-B1": ("evidence_builders.py", _appended("for source_session in xs: pass")),
    "P20-B2": ("evidence_builders.py", _appended("del source_session")),
    "P9-B3": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN.replace("(*, observation)", "(*, observation, source_session)"),
    ),
    "P12-B4": ("evidence_builders.py", "import x as source_session\n" + _VALID_SESSION_SPECIMEN),
    "P11-B5": (
        "evidence_builders.py",
        _appended("try:\n    pass\nexcept E as source_session:\n    pass"),
    ),
    "P13-B6": (
        "evidence_builders.py",
        _appended("match v:\n    case source_session:\n        pass"),
    ),
    "P21-B7": (
        "evidence_builders.py",
        _appended("match v:\n    case [*source_session]:\n        pass"),
    ),
    "P22-B8": (
        "evidence_builders.py",
        _appended("match v:\n    case {**source_session}:\n        pass"),
    ),
    "P23-B9": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN + "\ndef other():\n    global source_session\n",
    ),
    "P14-R6": ("evidence_builders.py", _appended("observation = other")),
    "P15-R1": ("acceptance.py", _VALID_SESSION_SPECIMEN),
    "P16-R4": ("evidence_builders.py", _appended("c = source_session.active")),
    "P18-R2": (
        "evidence_builders.py",
        _VALID_SESSION_SPECIMEN.replace(_B_LINE, "")
        + "\ndef helper():\n    return source_session.elapsed_monotonic_seconds\n",
    ),
}
#: Further members sharing a representative's branch; each must also be refused.
_SESSION_SHARED_PROBES: dict[str, str] = {
    "B1-with": _appended("with ctx as source_session:\n    pass"),
    "B1-comprehension": _appended("[source_session for source_session in xs]"),
    "B1-augassign": _appended("source_session += 0"),
    "B1-walrus": _appended("(source_session := 0)"),
    "B1-annassign": _appended("source_session: int = 0"),
    "B3-lambda": _appended("lambda source_session: 0"),
    "B9-nonlocal": _appended("def _inner():\n    nonlocal source_session"),
    "R4-P17-missing-read": _VALID_SESSION_SPECIMEN.replace(_B_LINE, ""),
}


def test_session_read_exception_is_exact_and_receiver_pinned() -> None:
    """T-S9b: the valid specimen passes; every single-property probe is refused."""
    assert (
        _session_read_violations("evidence_builders.py", ast.parse(_VALID_SESSION_SPECIMEN)) == []
    )
    nested = _VALID_SESSION_SPECIMEN.replace(
        _B_LINE,
        "    def _inner():\n        return source_session.elapsed_monotonic_seconds\n",
    )
    assert _session_read_violations("evidence_builders.py", ast.parse(nested)) == []
    for source in _SESSION_SHARED_PROBES.values():
        assert _session_read_violations("evidence_builders.py", ast.parse(source)) != []


@pytest.mark.parametrize("probe", list(_SESSION_PROBES))
def test_session_read_probe_is_refused(probe: str) -> None:
    """T-S9b: each representative probe breaches exactly its named rule or branch."""
    module_name, source = _SESSION_PROBES[probe]
    assert _session_read_violations(module_name, ast.parse(source)) != []


_VALID_POLICY_SPECIMEN = """
def evaluate_tick(record, *, established_session_id):
    retained_session = record.session
    a = retained_session.active
    b = retained_session.elapsed_monotonic_seconds
"""
_POLICY_B_LINE = "    b = retained_session.elapsed_monotonic_seconds\n"

#: #954 slice 4f-c probes E1-E5 over the engine-policy site; each names its rule.
_POLICY_PROBES: dict[str, tuple[str, str, str]] = {
    "E1-R1-engine": ("engine.py", _VALID_POLICY_SPECIMEN, "R1:"),
    "E2-R3-receiver": (
        "engine_policy.py",
        _VALID_POLICY_SPECIMEN.replace("a = retained_session.active", "a = record.session.active"),
        "R3:",
    ),
    "E3-R5a-count": (
        "engine_policy.py",
        _VALID_POLICY_SPECIMEN + "    retained_session = record.session\n",
        "R5a:",
    ),
    "E4-R6-parameter": (
        "engine_policy.py",
        _VALID_POLICY_SPECIMEN.replace("(record, *,", "(other, *,"),
        "R6:",
    ),
    "E5-R2-function": (
        "engine_policy.py",
        _VALID_POLICY_SPECIMEN.replace(_POLICY_B_LINE, "")
        + "\ndef helper():\n    return retained_session.elapsed_monotonic_seconds\n",
        "R2:",
    ),
}


def test_session_read_sites_table_is_closed_and_immutable() -> None:
    """T21: the site table is exactly the two governed modules and cannot be mutated."""
    assert dict(_SESSION_READ_SITES) == {
        "evidence_builders.py": ("build_tick_record", "source_session", "observation"),
        "engine_policy.py": ("evaluate_tick", "retained_session", "record"),
    }
    with pytest.raises(TypeError):
        _SESSION_READ_SITES["engine.py"] = _SessionReadSite(  # type: ignore[index]
            "x", "y", "z"
        )


def test_engine_policy_session_specimen_is_admitted() -> None:
    """T21 E6: a valid ``engine_policy.py`` specimen yields no violation."""
    assert _session_read_violations("engine_policy.py", ast.parse(_VALID_POLICY_SPECIMEN)) == []


@pytest.mark.parametrize("probe", list(_POLICY_PROBES))
def test_engine_policy_session_probe_is_refused(probe: str) -> None:
    """T21 E1-E5: each engine-policy probe is refused by its named rule."""
    module_name, source, rule = _POLICY_PROBES[probe]
    violations = _session_read_violations(module_name, ast.parse(source))
    assert any(violation.startswith(rule) for violation in violations), violations


def test_engine_carries_the_heartbeat_only_under_the_renamed_decision_field() -> None:
    """T21/A1: ``engine.py`` passes T-S9 and reads only ``next_previous_elapsed_seconds``."""
    tree = _cold_production_trees()["engine.py"]
    names = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
    assert _session_read_violations("engine.py", tree) == []
    assert "next_previous_elapsed_seconds" in names
    assert "elapsed_monotonic_seconds" not in names


# --- #997 S1: closed version-1 temperature projection admission ---------------

_TEMPERATURE_MESSAGE = "MCP tick temperature projection failed strict admission"
_TEMPERATURE_KEY = "cold_" + "temperature_projection"


async def _temperature_error(payload: dict[str, object]) -> ColdTickTemperatureProjectionError:
    """Read one tolerant-admitted tick and return its temperature admission error."""
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)
    with pytest.raises(ColdTickTemperatureProjectionError) as raised:
        await client.get_roast_state()
    assert len(_state_calls(caller)) == 1
    assert isinstance(raised.value, ColdMcpValidationError)
    assert raised.value.args == (_TEMPERATURE_MESSAGE,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw"), ACCEPTED_SHAPES, ids=[case[0] for case in ACCEPTED_SHAPES]
)
async def test_t9_public_read_returns_the_exact_temperature_projection(
    name: str, raw: dict[str, object]
) -> None:
    """T9: each admitted shape reaches the observation unchanged from one read."""
    del name
    payload = _cold_state_payload()
    payload[_TEMPERATURE_KEY] = raw
    _assert_tolerant_mirror_accepts(payload)
    client, caller = await _started_client(payload)

    observation = await client.get_roast_state()

    temperature = observation.temperature
    assert type(temperature) is ColdTickTemperatureProjection
    for field in FIELD_NAMES:
        value: object = getattr(temperature, field)
        expected = raw[field]
        if isinstance(value, Enum):
            assert value.value == expected, field
        else:
            assert type(value) is type(expected), field
            assert value == expected, field
    assert _state_calls(caller) == [("get_roast_state", {"session_id": "session-id"})]


@pytest.mark.asyncio
async def test_t10_missing_projection_key_fails_closed_without_fallback() -> None:
    """T10: a response lacking the key (published 0.2.2) is refused; no fallback."""
    payload = _cold_state_payload()
    del payload[_TEMPERATURE_KEY]

    error = await _temperature_error(payload)

    assert error.failure is ColdTemperatureProjectionFailure.PROJECTION_KEY_MISSING


_ADAPTER_REPRESENTATIVES = (
    "null",
    "list",
    "version-true",
    "extra-key",
    "outcome-case",
    "counter-bool",
    "reported-auto",
    "counter-negative",
    "shape",
)


def _adapter_cases() -> list[tuple[str, object, ColdTemperatureProjectionFailure]]:
    """Return one JSON-safe leaf failure case per leaf failure member."""
    by_name = {name: (raw, expected) for name, raw, expected in LEAF_FAILURES}
    return [(name, *by_name[name]) for name in _ADAPTER_REPRESENTATIVES]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "expected"), _adapter_cases(), ids=list(_ADAPTER_REPRESENTATIVES)
)
async def test_t10_every_leaf_failure_surfaces_through_the_adapter(
    name: str, raw: object, expected: ColdTemperatureProjectionFailure
) -> None:
    """T10: each leaf failure member surfaces unchanged as the closed adapter error."""
    del name
    payload = _cold_state_payload()
    payload[_TEMPERATURE_KEY] = raw

    error = await _temperature_error(payload)

    assert error.failure is expected


def test_t10_adapter_representatives_cover_every_leaf_failure() -> None:
    """T10: the adapter corpus reaches every failure member except the adapter-only one."""
    reached = {expected for _, _, expected in _adapter_cases()}
    assert reached == set(ColdTemperatureProjectionFailure) - {
        ColdTemperatureProjectionFailure.PROJECTION_KEY_MISSING
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["absent", "null", "junk"])
async def test_t11_start_response_projection_is_never_read(variant: str) -> None:
    """T11: start-response null means not reported; the start never reads the projection."""
    start = _cold_start_payload()
    session = cast("dict[str, object]", start["session"])
    session.pop(_TEMPERATURE_KEY, None)
    if variant == "null":
        session[_TEMPERATURE_KEY] = None
    elif variant == "junk":
        session[_TEMPERATURE_KEY] = {"junk": 1}
    caller = _MappingCaller(
        {"start_roast_session": start, "get_roast_state": _cold_state_payload()}
    )
    client = ColdCharacterisationMCPClient(caller)

    result = await client.start_cold_session()

    assert result.session.session_id == "session-id"
    observation = await client.get_roast_state()
    assert type(observation.temperature) is ColdTickTemperatureProjection


def test_t12_tolerant_mirror_gains_no_temperature_field() -> None:
    """T12: the tolerant top-level mirror stays unchanged; only the cold path admits."""
    assert _TEMPERATURE_KEY not in RoastSessionState.model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        ("identity", ColdSessionIdentityError),
        ("purpose", ColdSessionPurposeError),
        ("audio", ColdTickAudioProjectionError),
        ("device", ColdTickDeviceProjectionError),
        ("roast_fan", ColdTickRoastFanProjectionError),
        ("session", ColdTickSessionProjectionError),
    ],
)
async def test_t12_earlier_cold_tick_checks_precede_temperature_admission(
    earlier: str, expected: type[ColdMcpError]
) -> None:
    """T12: identity, purpose, audio, device, roast fan, and session win over temperature."""
    payload = _cold_state_payload()
    payload[_TEMPERATURE_KEY] = celsius_agree(projection_version=2)
    if earlier == "identity":
        payload["session_id"] = "other-session"
    elif earlier == "purpose":
        payload["session_purpose"] = "roast"
    elif earlier == "audio":
        del _first_crack(payload)["max_consecutive_overflow_count"]
    elif earlier == "device":
        _device(payload)["heat_level_percent"] = "0"
    elif earlier == "roast_fan":
        _roast_fan(payload)["roast_fan_level_percent"] = "0"
    else:
        payload[ColdSessionField.ACTIVE.value] = "true"
    client, caller = await _started_client(payload)

    with pytest.raises(ColdMcpError) as raised:
        await client.get_roast_state()

    assert type(raised.value) is expected
    assert len(_state_calls(caller)) == 1


@pytest.mark.asyncio
async def test_t12_foreign_session_with_a_valid_projection_is_refused_first() -> None:
    """T12: a valid projection never rescues a foreign session identity."""
    payload = _cold_state_payload()
    payload["session_id"] = "other-session"
    client, _ = await _started_client(payload)

    with pytest.raises(ColdSessionIdentityError):
        await client.get_roast_state()


@pytest.mark.asyncio
async def test_t12_null_roast_fan_stays_rejected_beside_a_valid_projection() -> None:
    """T12/AC7: a missing required observation stays rejected with a good temperature."""
    payload = _cold_state_payload()
    payload["cold_characterisation_observation"] = None
    client, _ = await _started_client(payload)

    with pytest.raises(ColdTickRoastFanProjectionError) as raised:
        await client.get_roast_state()

    assert raised.value.failure is ColdRoastFanProjectionFailure.OBSERVATION_NULL


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["extra-key", "token", "string-counter"])
async def test_t13_temperature_failure_contains_untrusted_values(location: str) -> None:
    """T13: a rejected key or value never reaches any rendering of the error."""
    canary = _runtime_canary()
    raw = celsius_agree()
    if location == "extra-key":
        raw[canary] = 1
        expected = ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    elif location == "token":
        raw["reported_temperature_unit"] = canary
        expected = ColdTemperatureProjectionFailure.TOKEN_NOT_ADMITTED
    else:
        raw["status_packet_count"] = canary
        expected = ColdTemperatureProjectionFailure.VALUE_TYPE_NOT_EXACT
    payload = _cold_state_payload()
    payload[_TEMPERATURE_KEY] = raw

    error = await _temperature_error(payload)

    assert error.failure is expected
    _assert_contained(error, canary)
    assert canary not in repr(error.failure)


@pytest.mark.asyncio
async def test_t14_observation_requires_an_exact_temperature_projection() -> None:
    """T14: omission, ``None``, or a subclass instance is refused on direct construction."""
    client, _ = await _started_client(_cold_state_payload())
    observation = await client.get_roast_state()

    class ProjectionSubclass(ColdTickTemperatureProjection):
        """A non-exact projection type."""

    subclass = ProjectionSubclass.model_validate(dict(observation.temperature))
    parts: dict[str, object] = {
        "state": observation.state,
        "audio": observation.audio,
        "device": observation.device,
        "roast_fan": observation.roast_fan,
        "session": observation.session,
    }
    assert ColdTickObservation.model_fields["temperature"].is_required()
    with pytest.raises(ValidationError):
        ColdTickObservation.model_validate(parts)
    for bad in (None, subclass, dict(observation.temperature)):
        with pytest.raises(ValidationError):
            ColdTickObservation.model_validate({**parts, "temperature": bad})
    rebuilt = ColdTickObservation.model_validate({**parts, "temperature": observation.temperature})
    assert rebuilt == observation


def test_t15_cold_production_code_reads_the_temperature_key_once() -> None:
    """T15: one raw subscript in ``mcp.py`` is the only production access to the key."""
    attributes: list[str] = []
    getattr_calls: list[str] = []
    constants: dict[str, list[ast.Constant]] = {}
    trees = _cold_production_trees()
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == _TEMPERATURE_KEY:
                attributes.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == _TEMPERATURE_KEY
                    for arg in node.args
                )
            ):
                getattr_calls.append(name)
            if isinstance(node, ast.Constant) and node.value == _TEMPERATURE_KEY:
                constants.setdefault(name, []).append(node)
    assert "temperature_projection.py" in trees
    assert attributes == []
    assert getattr_calls == []
    assert sorted(constants) == ["mcp.py"]
    assert len(constants["mcp.py"]) == 1
    subscripts = [
        node
        for node in ast.walk(trees["mcp.py"])
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == _TEMPERATURE_KEY
    ]
    assert len(subscripts) == 1
    assert subscripts[0].slice is constants["mcp.py"][0]
