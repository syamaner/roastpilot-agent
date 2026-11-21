"""Pure decoder proof for the ratified #1111 bootstrap inventory amendment.

Synthetic wire values only; no transport, discovery, Start or hardware authority.
"""

import json
from enum import Enum
from typing import cast

import pytest

from roastpilot_agent.human_bootstrap import (
    BOOTSTRAP_TOOL_INVENTORY,
    BootstrapInventory,
    BootstrapRead,
    decode_bootstrap_reply,
)

# Independent literal expectations pin names and order, including the insertion.
LEGACY = (
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
    "finalise_cold_characterisation_session",
)
T3A = (*LEGACY[:9], "drop_beans_exact_session", *LEGACY[9:])
MODES = [(BootstrapInventory.LEGACY, LEGACY), (BootstrapInventory.T3A_MANUAL_MOCK, T3A)]


def _server(tools: object) -> dict[str, object]:
    return {
        "product_name": "RoastPilot",
        "package_name": "coffee-roaster-mcp",
        "version": "0.2.4",
        "transport": "stdio",
        "current_phase": "bootstrap",
        "roaster_driver": "mock",
        "first_crack_mode": "audio",
        "bootstrap_safe": False,
        "session_presence": "none",
        "available_bootstrap_tools": tools,
        "started_at_utc": "2026-01-01T00:00:00+00:00",
    }


def _decode(
    structured: object,
    *,
    inventory: BootstrapInventory = BootstrapInventory.LEGACY,
    text: str | None = None,
    read: BootstrapRead = BootstrapRead.SERVER_INFO,
    is_error: object = False,
    config_present: bool = False,
) -> dict[str, object] | None:
    return decode_bootstrap_reply(
        read,
        structured=structured,
        content=[{"type": "text", "text": json.dumps(structured) if text is None else text}],
        is_error=is_error,
        config_present=config_present,
        inventory=inventory,
    )


def test_legacy_default_and_plain_closed_enum() -> None:
    """Omitted selection preserves the exact original inventory."""
    assert BOOTSTRAP_TOOL_INVENTORY == LEGACY
    assert set(BootstrapInventory.__members__) == {"LEGACY", "T3A_MANUAL_MOCK"}
    assert not isinstance(BootstrapInventory.LEGACY, str)
    payload = _server(list(LEGACY))
    # Call the public decoder directly to prove its own default, not the helper's.
    assert (
        decode_bootstrap_reply(
            BootstrapRead.SERVER_INFO,
            structured=payload,
            content=[{"type": "text", "text": json.dumps(payload)}],
            is_error=False,
            config_present=False,
        )
        == payload
    )
    assert (
        decode_bootstrap_reply(
            BootstrapRead.SERVER_INFO,
            structured=_server(list(T3A)),
            content=[{"type": "text", "text": json.dumps(_server(list(T3A)))}],
            is_error=False,
            config_present=False,
        )
        is None
    )


@pytest.mark.parametrize(("inventory", "tools"), MODES)
def test_exact_inventory_only_in_its_selected_mode(
    inventory: BootstrapInventory, tools: tuple[str, ...]
) -> None:
    """Each mode accepts its exact tuple and refuses the other without fallback."""
    payload = _server(list(tools))
    assert _decode(payload, inventory=inventory) == payload
    other = T3A if inventory is BootstrapInventory.LEGACY else LEGACY
    assert _decode(_server(list(other)), inventory=inventory) is None


def _invalid_tools(tools: tuple[str, ...]) -> list[object]:
    return [
        list(reversed(tools)),
        [*tools[:8], tools[9], tools[8], *tools[10:]],
        [*tools, "arbitrary_tool"],
        list(tools[:-1]),
        [*tools[:8], tools[8], *tools[8:]],
        [*tools[:8], "arbitrary_tool", *tools[9:]],
        [*tools[:8], 1, *tools[9:]],
        [*tools[:8], True, *tools[9:]],
        [*tools[:8], None, *tools[9:]],
        ",".join(tools),
        {str(index): name for index, name in enumerate(tools)},
        None,
    ]


@pytest.mark.parametrize(("inventory", "tools"), MODES)
@pytest.mark.parametrize("wire", ["structured", "text", "both"])
def test_invalid_inventory_on_either_wire_is_refused(
    inventory: BootstrapInventory, tools: tuple[str, ...], wire: str
) -> None:
    """Wrong names, order, count and raw types cannot be coerced or masked."""
    for invalid in _invalid_tools(tools):
        good = _server(list(tools))
        bad = _server(invalid)
        structured = bad if wire in {"structured", "both"} else good
        text_value = bad if wire in {"text", "both"} else good
        assert _decode(structured, text=json.dumps(text_value), inventory=inventory) is None


@pytest.mark.parametrize(("inventory", "tools"), MODES)
def test_structured_tuple_is_not_coerced(
    inventory: BootstrapInventory, tools: tuple[str, ...]
) -> None:
    """A Python tuple is invalid in structured data even though JSON renders a list."""
    assert _decode(_server(tools), inventory=inventory) is None


@pytest.mark.parametrize(("inventory", "tools"), MODES)
def test_divergent_valid_shapes_and_duplicate_key_text_refused(
    inventory: BootstrapInventory, tools: tuple[str, ...]
) -> None:
    """Independently valid shapes must agree; duplicate text keys stay invalid."""
    payload = _server(list(tools))
    divergent = {**payload, "version": "different"}
    assert _decode(payload, text=json.dumps(divergent), inventory=inventory) is None
    for key in ("available_bootstrap_tools", "version"):
        duplicate = json.dumps(payload)[:-1] + f", {json.dumps(key)}: {json.dumps(payload[key])}}}"
        assert _decode(payload, text=duplicate, inventory=inventory) is None
    assert _decode(payload, text="{", inventory=inventory) is None
    assert _decode(payload, is_error=True, inventory=inventory) is None


@pytest.mark.parametrize(("inventory", "tools"), MODES)
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("product_name", "other"),
        ("package_name", "other"),
        ("transport", "http"),
        ("current_phase", "pre_roast"),
        ("roaster_driver", "hottop"),
        ("first_crack_mode", "manual"),
        ("session_presence", "active"),
        ("bootstrap_safe", 0),
        ("version", ""),
        ("started_at_utc", None),
        ("extra", "field"),
    ],
)
def test_inventory_selection_preserves_closed_server_shape(
    inventory: BootstrapInventory, tools: tuple[str, ...], key: str, value: object
) -> None:
    """Selecting a new inventory changes no SERVER_INFO constant or field rule."""
    payload = _server(list(tools))
    payload[key] = value
    assert _decode(payload, inventory=inventory) is None
    payload = _server(list(tools))
    del payload[key if key in payload else "version"]
    assert _decode(payload, inventory=inventory) is None


class _ForeignInventory(Enum):
    LEGACY = "legacy"


@pytest.mark.parametrize(
    "invalid", [None, "legacy", "t3a_manual_mock", (), 0, _ForeignInventory.LEGACY]
)
@pytest.mark.parametrize("tools", [LEGACY, T3A])
def test_inventory_requires_exact_enum_at_runtime(tools: tuple[str, ...], invalid: object) -> None:
    """No arbitrary allowed-tools argument or enum-like selector is accepted."""
    assert _decode(_server(list(tools)), inventory=cast("BootstrapInventory", invalid)) is None


@pytest.mark.parametrize("inventory", list(BootstrapInventory))
def test_runtime_and_capture_grammars_remain_strict(inventory: BootstrapInventory) -> None:
    """Inventory choice leaves configuration and capture requirements intact."""
    runtime: dict[str, object] = {
        "config_source": None,
        "roaster_driver": "mock",
        "roaster_port": None,
        "roaster_baudrate": 115200,
        "temperature_unit": "celsius",
        "command_interval_seconds": 0.3,
        "first_crack_mode": "audio",
        "first_crack_event_authority": "manual",
        "first_crack_observer_protocol": "manual/v1",
        "model_repo_id": "test-org/test-model",
        "model_precision": "int8",
        "allow_manual_override": True,
        "log_dir": "test-logs",
        "sample_interval_seconds": 1.0,
        "auto_t0_detection_enabled": True,
        "auto_t0_drop_threshold_c": 25.0,
    }
    assert _decode(runtime, read=BootstrapRead.RUNTIME_CONFIG, inventory=inventory) == runtime
    assert (
        _decode(
            runtime, read=BootstrapRead.RUNTIME_CONFIG, inventory=inventory, config_present=True
        )
        is None
    )
    for key, value in [("temperature_unit", "fahrenheit"), ("allow_manual_override", 1)]:
        assert (
            _decode({**runtime, key: value}, read=BootstrapRead.RUNTIME_CONFIG, inventory=inventory)
            is None
        )
    capture = {
        "capture_protocol": "capture_evidence/v1",
        "intent": "bootstrap",
        "capability": "manual_mock_capture",
    }
    for invalid in (None, "legacy", "t3a_manual_mock", (), 0, _ForeignInventory.LEGACY):
        assert (
            _decode(
                runtime,
                read=BootstrapRead.RUNTIME_CONFIG,
                inventory=cast("BootstrapInventory", invalid),
            )
            is None
        )
        assert (
            _decode(
                {"result": capture},
                text=json.dumps(capture),
                read=BootstrapRead.CAPTURE_EVIDENCE,
                inventory=cast("BootstrapInventory", invalid),
            )
            is None
        )
    assert (
        _decode(
            {"result": capture},
            text=json.dumps(capture),
            read=BootstrapRead.CAPTURE_EVIDENCE,
            inventory=inventory,
        )
        == capture
    )
    assert _decode(capture, read=BootstrapRead.CAPTURE_EVIDENCE, inventory=inventory) is None
    assert (
        _decode(
            {"result": {**capture, "capability": "other"}},
            text=json.dumps({**capture, "capability": "other"}),
            read=BootstrapRead.CAPTURE_EVIDENCE,
            inventory=inventory,
        )
        is None
    )
