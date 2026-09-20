"""Behavioural safety tests for the cold-characterisation MCP boundary."""

import inspect
import json
import traceback
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation.mcp import (
    COLD_ALLOWED_TOOLS,
    ColdCharacterisationMCPClient,
    ColdFinalisationNotCleanError,
    ColdFinalisationSafetyError,
    ColdMcpError,
    ColdMcpTransportError,
    ColdMcpValidationError,
    ColdModeForbiddenToolError,
    ColdSessionIdentityError,
    ColdSessionPhaseError,
    ColdSessionPurposeError,
    RejectionReason,
    SessionFinalisationResult,
    _finalisation_has_capability_compatible_evidence,  # pyright: ignore[reportPrivateUsage]
    finalisation_is_clean,
)
from roastpilot_agent.mcp_client import MCPConnectionError, MCPToolError, MCPToolTimeoutError

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
    assert (await client.get_roast_state()).session_id == "session-id"
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
