"""Behavioural safety tests for the cold-characterisation MCP boundary."""

import inspect
import json
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


def _client_for(result: object) -> tuple[ColdCharacterisationMCPClient, _Caller]:
    """Build a cold client with a deterministic result supplier."""
    caller = _Caller(result)
    return ColdCharacterisationMCPClient(caller), caller


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
async def test_start_cold_session_requires_confirmed_purpose() -> None:
    """G3: a defaulted or wrong session purpose cannot enter cold mode."""
    start_payload = json.loads((_MCP_TOOL_FIXTURES / "start_roast_session.json").read_text())
    start_payload["session"]["session_purpose"] = "roast"
    client, caller = _client_for(start_payload)

    with pytest.raises(ColdSessionPurposeError):
        await client.start_cold_session()
    assert caller.calls == [("start_roast_session", {"purpose": "cold_characterisation"})]


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
    session = cast(
        "dict[str, object]",
        json.loads((_MCP_TOOL_FIXTURES / "start_roast_session.json").read_text()),
    )
    session_state = cast("dict[str, object]", session["session"])
    session_state["session_purpose"] = "cold_characterisation"
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
@pytest.mark.parametrize("reason", list(RejectionReason))
async def test_every_closed_rejection_reason_cannot_be_clean(reason: RejectionReason) -> None:
    """G5: every accepted closed rejection value rejects the finalisation."""
    payload = _payload()
    payload["rejection_reason"] = reason.value
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
    """Client callers receive a fixed typed error instead of a raw Pydantic failure."""
    payload = _payload()
    payload["clean"] = "true"
    client, _ = _client_for(payload)
    with pytest.raises(
        ColdMcpValidationError, match="MCP response failed cold contract validation"
    ) as raised:
        await client.finalise_session("session-id")
    assert isinstance(raised.value.__cause__, ValidationError)


def test_capability_branch_has_one_predicate_and_no_driver_name_path() -> None:
    """Class H: source shape precludes a second or name-derived branch."""
    source = Path(inspect.getfile(ColdCharacterisationMCPClient)).read_text()
    assert source.count("if _command_streaming_required(evidence):") == 1
    assert "driver_name" not in source
    assert "driver_type" not in source
